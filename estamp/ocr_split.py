"""Split a vendor PDF into single papers, and read each paper's text.

The run context holds a split client and an OCR client (`Ctx.splitter`, `Ctx.ocr`); intake and the issue check
only go through those, so tests swap in fakes. The default clients below use the providers configured here.

Providers (config.yaml → intake):
  split_provider: local     – pypdf, every `pages_per_paper` pages = one paper (works today)
                  zoop_api  – your in-house split API (fill zoop_api.split once the contract is final)
  ocr_provider:   drive     – Google Drive OCR (works today, ~3–5 s per paper)
                  zoop_api  – your in-house OCR API; falls back to Drive if ocr_fallback_to_drive
"""
from __future__ import annotations

import base64
import io
import re
from dataclasses import dataclass

import requests
from pypdf import PdfReader, PdfWriter

from .settings import Settings


@dataclass
class PaperPdf:
    page_from: int      # 1-based
    page_to: int
    data: bytes


@dataclass
class OcrResult:
    text: str
    cert: str           # only when the in-house API returns it directly
    via: str


def _json_path(obj, path: str):
    if not path:
        return None
    for part in path.split("."):
        if obj is None:
            return None
        obj = obj[int(part)] if isinstance(obj, list) and part.isdigit() else (obj.get(part) if isinstance(obj, dict) else None)
    return obj


def _headers(settings: Settings, section: str) -> dict:
    key = settings.env(f"zoop_api.{section}.api_key_env")
    if not key:
        raise RuntimeError(f"API key env var for zoop_api.{section} is not set")
    return {settings.get(f"zoop_api.{section}.auth_header", "x-api-key"): key}


# ---------------------------------------------------------------------------------- split

def split_pdf(data: bytes, name: str, settings: Settings) -> list[PaperPdf]:
    """zoop_api → already one PDF per paper. local → ONE PDF PER PAGE; intake groups pages into papers
    by content (a physical paper's back page stays with its front)."""
    provider = settings.get("intake.split_provider", "local")
    if provider == "zoop_api":
        return _split_zoop(data, name, settings)
    return _split_local(data, 1)


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


def _split_local(data: bytes, per: int) -> list[PaperPdf]:
    reader = PdfReader(io.BytesIO(data))
    n = len(reader.pages)
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


def _split_zoop(data: bytes, name: str, settings: Settings) -> list[PaperPdf]:
    c = settings.get("zoop_api.split")
    res = requests.post(c["url"], headers=_headers(settings, "split"),
                        files={c.get("file_field", "file"): (name, data, "application/pdf")},
                        data=c.get("extra_fields") or {}, timeout=int(c.get("timeout_s", 300)))
    res.raise_for_status()
    papers = _json_path(res.json(), c.get("papers_path", "data.papers")) or []
    out = []
    for i, p in enumerate(papers):
        pages = p.get(c.get("pages_field", "pages")) or [i + 1]
        out.append(PaperPdf(min(pages), max(pages), base64.b64decode(p[c.get("pdf_b64_field", "pdf_base64")])))
    if not out:
        raise RuntimeError("split API returned no papers")
    return out


# ---------------------------------------------------------------------------------- OCR

def pdf_text(data: bytes) -> str:
    """Text already inside the PDF (digital e-stamps). Empty for scans."""
    try:
        return "\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return ""


def read_paper(data: bytes, name: str, settings: Settings, google) -> OcrResult:
    # 1. Digital PDF (e-stamp): use its built-in text – instant and exact, no OCR needed.
    if settings.get("intake.use_pdf_text", True):
        layer = pdf_text(data)
        if len(re.sub(r"\s+", "", layer)) >= int(settings.get("intake.min_pdf_text_chars", 40)):
            return OcrResult(layer, "", "pdf-text")
    # 2. Scanned page: OCR.
    provider = settings.get("intake.ocr_provider", "drive")
    if provider == "zoop_api":
        try:
            return _ocr_zoop(data, name, settings)
        except Exception as e:
            if not settings.get("intake.ocr_fallback_to_drive", True):
                raise
            return OcrResult(google.ocr_text(data, name), "", f"drive (API failed: {str(e)[:100]})")
    return OcrResult(google.ocr_text(data, name), "", "drive")


class ConfiguredOcr:
    """Default OCR client: reads a paper with the providers set in config."""

    def __init__(self, settings: Settings, google):
        self.settings, self.google = settings, google

    def read(self, data: bytes, name: str) -> OcrResult:
        return read_paper(data, name, self.settings, self.google)


class ConfiguredSplitter:
    """Default split client: splits an upload with the provider set in config."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def split(self, data: bytes, name: str) -> list[PaperPdf]:
        return split_pdf(data, name, self.settings)


def _ocr_zoop(data: bytes, name: str, settings: Settings) -> OcrResult:
    c = settings.get("zoop_api.ocr")
    if not c.get("url"):
        raise RuntimeError("zoop_api.ocr.url is not set")
    res = requests.post(c["url"], headers=_headers(settings, "ocr"),
                        files={c.get("file_field", "file"): (name, data, "application/pdf")},
                        data=c.get("extra_fields") or {}, timeout=int(c.get("timeout_s", 60)))
    if res.status_code >= 300:
        raise RuntimeError(f"OCR API HTTP {res.status_code}: {res.text[:200]}")
    body = res.json()
    text = str(_json_path(body, c.get("text_path", "")) or "")
    cert = str(_json_path(body, c.get("cert_path", "")) or "").strip().upper()
    if not text and not cert:
        raise RuntimeError("OCR API returned neither text nor certificate number")
    return OcrResult(text, cert, "zoop_api")
