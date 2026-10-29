"""Split a vendor PDF into fixed-size page parts, and read each part's text. Parts are NOT papers yet:
intake groups the parts into papers (a traditional paper's back page stays with its front).

The run context holds a split client and an OCR client (`Ctx.splitter`, `Ctx.ocr`); intake and the issue check
only go through those, so tests swap in fakes. The default clients below use the providers configured here.

Providers (config.yaml → intake):
  split_provider: local     – pypdf (works today)
                  zoop_api  – in-house split API, contract in docs/api-samples/split/README.md (no auth)
  Both split into parts of 1 page (digital e-stamps) or 2 pages (traditional papers: front + back).
  ocr_provider:   zoop_api  – your in-house OCR API (the only reader; on failure it raises, never falls back – D1)
"""
from __future__ import annotations

import base64
import io
import re
import zipfile
from dataclasses import dataclass

import requests
from pypdf import PdfReader, PdfWriter
from pypdf.errors import FileNotDecryptedError

from .http_api import api_call
from .settings import Settings


@dataclass
class PaperPdf:
    page_from: int      # 1-based
    page_to: int
    data: bytes


@dataclass
class OcrResult:
    text: str
    cert: str                   # an ID a reader returned outside its text (legacy)
    via: str
    fields: dict | None = None  # OCR API: structured fields (it returns no text); None for text readers
    raw: dict | None = None     # OCR API: the full response, stored per paper


# ---------------------------------------------------------------------------------- split

def split_pdf(data: bytes, name: str, settings: Settings, split_size: int = 1) -> list[PaperPdf]:
    """Split into parts of `split_size` pages (1 = digital e-stamp, 2 = traditional front + back), via the
    in-house split API (zoop_api) or pypdf (local). Intake picks the size from the upload's paper type."""
    if settings.get("intake.split_provider", "local") == "zoop_api":
        return _split_zoop(data, name, settings, split_size)
    return _split_local(data, split_size)


def merge_pdfs(parts: list[bytes]) -> bytes:
    if len(parts) == 1:
        return parts[0]
    w = PdfWriter()
    for part in parts:
        for page in PdfReader(io.BytesIO(part)).pages:
            w.add_page(page)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


class PdfUnreadable(Exception):
    """pypdf can't open the file (password-protected, corrupt, no pages). Retrying never helps – a human must
    get a clean file (M-4)."""


def _split_local(data: bytes, per: int) -> list[PaperPdf]:
    try:
        reader = PdfReader(io.BytesIO(data))
        n = len(reader.pages)
        if n == 0:
            raise PdfUnreadable("PDF has no pages – get a clean file from the vendor and upload it again")
        if n <= per:
            return [PaperPdf(1, n, data)]
        out = []
        for start in range(0, n, per):
            w = PdfWriter()
            for i in range(start, min(start + per, n)):
                w.add_page(reader.pages[i])
            buf = io.BytesIO()
            w.write(buf)
            out.append(PaperPdf(start + 1, min(start + per, n), buf.getvalue()))
        return out
    except PdfUnreadable:
        raise
    except FileNotDecryptedError:
        raise PdfUnreadable("PDF is password-protected – remove the password and re-upload")
    except Exception as e:      # pypdf raises many types on a malformed file
        raise PdfUnreadable(f"PDF can't be opened ({type(e).__name__}) – get a clean file from the vendor and "
                            "upload it again")


_PART_NAME = re.compile(r"-(\d+)-(\d+)\.pdf$")


def _split_zoop(data: bytes, name: str, settings: Settings, split_size: int = 1) -> list[PaperPdf]:
    """In-house split API (contract: docs/api-samples/split/README.md). Returns a zip with one PDF per part,
    named `<prefix>-<from>-<to>.pdf`. No auth. The parts must cover pages 1..N exactly once."""
    c = settings.get("zoop_api.split") or {}
    if not c.get("url"):
        raise RuntimeError("zoop_api.split.url is not set")
    n = len(PdfReader(io.BytesIO(data)).pages)
    if n == 0:   # never let an empty file count as "processed, 0 papers"
        raise RuntimeError("PDF has no pages")
    timeout = int(c.get("timeout_s", 300))
    try:
        res = api_call(settings, "split", lambda: requests.post(
            c["url"], files={"file": (name, data, "application/pdf")},
            data={"ranges[0][startPage]": "1", "ranges[0][endPage]": str(n), "splitSize": str(split_size)},
            timeout=timeout))
    except requests.Timeout:
        raise RuntimeError(f"split API timed out after {timeout} s")
    except requests.RequestException as e:
        raise RuntimeError(f"split API unreachable ({type(e).__name__})")
    if res.status_code != 200:
        try:
            msg = res.json().get("message", "")
        except Exception:
            msg = ""
        raise RuntimeError(f"split API HTTP {res.status_code}: {msg or res.content[:200].decode('utf-8', 'replace')}")
    try:
        z = zipfile.ZipFile(io.BytesIO(res.content))
    except zipfile.BadZipFile:
        raise RuntimeError("split API returned something that is not a zip")
    if len(z.namelist()) > n:    # checked before decompressing anything
        raise RuntimeError(f"split API returned {len(z.namelist())} parts for a {n}-page file")
    out = []
    for entry in z.namelist():
        m = _PART_NAME.search(entry)
        if not m:
            raise RuntimeError(f"split API returned an unexpected part name '{entry}'")
        part = z.read(entry)
        p_from, p_to = int(m.group(1)), int(m.group(2))
        if len(PdfReader(io.BytesIO(part)).pages) != p_to - p_from + 1:
            raise RuntimeError(f"split API part '{entry}' does not have pages {p_from}-{p_to}")
        out.append(PaperPdf(p_from, p_to, part))
    out.sort(key=lambda p: p.page_from)
    covered = [pg for p in out for pg in range(p.page_from, p.page_to + 1)]
    if covered != list(range(1, n + 1)):
        raise RuntimeError(f"split API parts don't cover pages 1-{n} exactly once (got {covered})")
    return out


# ---------------------------------------------------------------------------------- OCR

def read_paper(data: bytes, name: str, settings: Settings) -> OcrResult:
    """Read one paper with the in-house OCR API only (D1). On failure it raises – never falls back to Drive OCR
    or the PDF text layer, so a wrong reading can't be silently issued."""
    return _ocr_zoop(data, name, settings)


class ConfiguredOcr:
    """Default OCR client: reads a paper with the OCR API set in config."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def read(self, data: bytes, name: str) -> OcrResult:
        return read_paper(data, name, self.settings)


class ConfiguredSplitter:
    """Default split client: splits an upload with the provider set in config."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def split(self, data: bytes, name: str, split_size: int = 1) -> list[PaperPdf]:
        return split_pdf(data, name, self.settings, split_size)


def _ocr_zoop(data: bytes, name: str, settings: Settings) -> OcrResult:
    """In-house OCR API, one page per call (contract: docs/api-samples/ocr/README.md). Returns structured fields only.
    'Not found' codes (211/212) are a normal reading – the paper checks report what is missing. Rate limit, retries
    and the per-page time cap (timeout_s per try, max_total_s overall) come from http_api / config."""
    c = settings.get("zoop_api.ocr") or {}
    # Production endpoint comes from .env (ESTAMP_OCR_URL); dev falls back to zoop_api.ocr.url in config.yaml.
    url = settings.env("zoop_api.ocr.url_env") or c.get("url")
    key, org = settings.env("zoop_api.ocr.api_key_env"), settings.env("zoop_api.ocr.org_acc_env")
    if not url or not c.get("config_id"):
        raise RuntimeError("OCR endpoint not set – set ESTAMP_OCR_URL in .env (or zoop_api.ocr.url) and zoop_api.ocr.config_id")
    if not key or not org:
        raise RuntimeError(f"{c.get('api_key_env')} / {c.get('org_acc_env')} must be set in .env")
    body = {"mode": "sync", "ocr_config_id": c["config_id"], "documents": [{"data": base64.b64encode(data).decode()}]}
    timeout = int(c.get("timeout_s", 60))
    try:
        res = api_call(settings, "ocr", lambda: requests.post(
            url, json=body, timeout=timeout,
            headers={"x-zoop-key": key, "x-zoop-org-acc": org, "Content-Type": "application/json"}))
    except requests.Timeout:
        raise RuntimeError(f"OCR API timed out after {timeout} s ('{name}')")
    except requests.RequestException as e:
        raise RuntimeError(f"OCR API unreachable ({type(e).__name__})")
    try:
        j = res.json()
    except ValueError:
        raise RuntimeError(f"OCR API HTTP {res.status_code} returned something that is not JSON")
    if res.status_code != 200 or not j.get("success"):
        err = j.get("error") if isinstance(j.get("error"), dict) else {}
        msg = err.get("message") or j.get("message") or res.content[:200].decode("utf-8", "replace")
        raise RuntimeError(f"OCR API HTTP {res.status_code}: {msg}")
    fields = ((j.get("data") or {}).get("provider_response") or {}).get("fields")
    if not isinstance(fields, dict):
        raise RuntimeError("OCR API returned an unexpected response (no data.provider_response.fields)")
    return OcrResult("", "", "zoop_api", fields=fields, raw=j)
