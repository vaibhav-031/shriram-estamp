"""Intake: ops drop vendor PDFs (merged or single) into the Upload Inbox. Per upload: paper type (digital /
traditional) → split (1 or 2 pages) → OCR every page → pairing check (traditional). Then for each paper:

  find cert no + state (from cert) + stamp duty → checks →
    OK   → Inventory/<STATE>/<AMOUNT>/<CERT>.pdf     (folders created automatically)
    FAIL → Needs Review/<date>/<upload>__p<page>.pdf  (reason in the ledger + dashboard)

A whole upload with a mixed/unclear type (E105) or an odd traditional page count (E106) moves untouched to
Needs Review/<date>/ (reason on the upload row + Log). The original upload is never deleted: it moves to
Upload Archive/<date>/.
Re-running is safe: pages already in the ledger are skipped.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .context import Ctx, log
from .db import UniqueViolation
from dataclasses import dataclass

from .extract import (PaperAnalysis, analyze_paper, first_broken_pair, identify, normalize_ocr, paper_type,
                      safe_file_name)
from .google_clients import FOLDER, PDF, parse_time
from .ocr_split import _split_local, merge_pdfs, page_text_chars


def _collect(g, folder_id: str, depth: int = 0, max_depth: int = 4) -> tuple[list[dict], list[dict]]:
    """All files in the inbox, including inside any folders ops uploaded (RJ/, Vendor batch 3/, …).
    Returns (files, subfolders). Folder names are ignored – every paper is routed by its certificate."""
    files, folders = [], []
    for item in g.list_children(folder_id):
        if item.get("mimeType") == FOLDER:
            if depth < max_depth:
                folders.append(item)
                sub_files, sub_folders = _collect(g, item["id"], depth + 1, max_depth)
                files += sub_files
                folders += sub_folders
        else:
            files.append(item)
    return files, folders


def run_intake(ctx: Ctx, deadline: float) -> None:
    g, s = ctx.google, ctx.settings
    inbox = s.folder("upload_inbox")
    settle = int(s.get("intake.settle_minutes", 5)) * 60
    files, folders = _collect(g, inbox)
    for f in sorted(files, key=lambda x: x["modifiedTime"]):
        if time.time() > deadline:
            ctx.event("INFO", "Intake", "time budget for intake reached – continuing next cycle")
            return
        age = (datetime.now(timezone.utc) - parse_time(f["modifiedTime"])).total_seconds()
        if age < settle:
            continue
        try:
            _process_upload(ctx, f)
        except Exception as e:
            ctx.event("ERROR", "Intake", f"'{f['name']}' failed: {e} – stays in the inbox and is retried next cycle",
                      ref=f["id"], alert_key=f"intakefail|{f['id']}")
    # Uploaded folders that are now empty go to the archive (deepest first), so the inbox stays clean.
    if not ctx.dry:
        archive = g.folder_path(s.folder("upload_archive"), [ctx.today().isoformat()])
        for fo in reversed(folders):
            try:
                if not g.list_children(fo["id"]):
                    g.move_rename(fo["id"], archive)
            except Exception as e:
                ctx.event("WARN", "Intake", f"could not archive empty folder '{fo['name']}': {e}")


@dataclass
class Unit:
    """One paper: one or more pages (front + back for physical stamp paper)."""
    page_from: int
    page_to: int
    parts: list[bytes]
    texts: list[str]
    api_cert: str = ""
    via: str = ""
    error: str = ""

    @property
    def data(self) -> bytes:
        return merge_pdfs(self.parts)

    @property
    def text(self) -> str:
        return "\n".join(self.texts)


class WholeUploadReview(Exception):
    """The whole upload goes to Needs Review untouched: E105 paper type unclear/mixed, E106 odd traditional pages."""

    def __init__(self, code: str, reason: str):
        super().__init__(f"{code}: {reason}")
        self.code, self.reason = code, reason


def _read_units(ctx: Ctx, data: bytes, name: str) -> list[Unit]:
    """Paper type → split by type → OCR every page (parallel) → papers (ADR-0003).

    digital     → 1 page per paper; each page must carry exactly one ID (checked by analyze_paper).
    traditional → 2 pages per paper (front + back); front and back are OCR'd separately. The first pair whose front
                  doesn't carry exactly one serial, or whose back carries one, breaks the pairing: that pair and
                  every later pair go to Needs Review (E107) – one missing page shifts every pair after it.
    Mixed/unclear type (E105) or an odd traditional page count (E106) → WholeUploadReview, nothing is split."""
    s = ctx.settings
    chars = page_text_chars(data)
    kind = paper_type(chars, int(s.get("intake.min_pdf_text_chars", 40)))   # same threshold as the reader
    if kind == "mixed":
        raise WholeUploadReview("E105", "paper type unclear – digital and scanned pages in one file. "
                                        "Split them into separate files and upload again")
    if kind == "traditional" and len(chars) % 2:
        raise WholeUploadReview("E106", f"scanned upload has {len(chars)} pages (odd) – every traditional paper "
                                        "needs front + back. Find the missing page and upload again")
    size = 1 if kind == "digital" else 2
    pieces = ctx.splitter.split(data, name, size)
    wrong = [p for p in pieces if p.page_to - p.page_from + 1 != size]
    if wrong:   # never stock a part that holds more (or less) than one paper – retried next cycle
        raise RuntimeError(f"split returned parts that are not {size} page(s) each "
                           f"(e.g. pages {wrong[0].page_from}-{wrong[0].page_to}) – nothing stocked")
    pages = [(p.page_from, p.data) for p in pieces] if kind == "digital" else \
        [(p.page_from + i, pg.data) for p in pieces for i, pg in enumerate(_split_local(p.data, 1))]
    ctx.event("INFO", "Intake", f"'{name}': {kind}, {len(pages)} page(s) in {len(pieces)} paper(s) – reading them now")
    done = [0]

    def read(page):
        no, pdf = page
        try:
            r = ctx.ocr.read(pdf, f"{name}_p{no}")
            out = (r.text, r.cert, r.via, "")
        except Exception as e:
            out = ("", "", "error", f"OCR failed on page {no}: {e}")
        with ctx._lock:
            done[0] += 1
            if done[0] % 10 == 0 or done[0] == len(pages):
                log.info(f"Intake: '{name}': read {done[0]}/{len(pages)}")
        return no, out

    with ThreadPoolExecutor(max_workers=int(s.get("workers", 8))) as ex:
        read_by_page = dict(ex.map(read, pages))

    ids = lambda t, c="": identify(normalize_ocr(t + ("\n" + c if c else "")), s, ctx.matcher, ctx.resolver)
    if kind == "digital":
        units = []
        for p in pieces:
            t, c, v, e = read_by_page[p.page_from]
            # A searchable scan of a traditional paper has a text layer too; never stock its front as 1 page.
            if not e and any(h.kind == "physical" for h in ids(t, c)):
                e = ("E105 page reads like a traditional paper (STATE-SERIAL) but the file is digital – not stocked "
                     "on its own, a back page could be lost. Check the page; scanned papers go in a separate file")
            units.append(Unit(p.page_from, p.page_to, [p.data], [t], c, v, e))
        return units

    units, counts = [], []
    for p in pieces:
        (tf, cf, vf, ef), (tb, cb, _, eb) = read_by_page[p.page_from], read_by_page[p.page_to]
        units.append(Unit(p.page_from, p.page_to, [p.data], [tf, tb], cf, vf, ef or eb))
        # A pair whose OCR failed can't prove or disprove the pairing; it goes to review on its own error.
        counts.append((1, 0) if ef or eb else (len(ids(tf, cf)), len(ids(tb, cb))))
    k = first_broken_pair(counts)
    if k is not None:
        at = pieces[k].page_from
        for u in units[k:]:
            u.error = (f"E107 pairing broken at page {at} – upload the papers from page {at} on again as separate "
                       f"2-page PDFs, then REJECT these entries")
    return units


def _analyze(ctx: Ctx, u: Unit):
    if u.error:
        return PaperAnalysis(issues=[u.error])
    return analyze_paper(u.text, u.api_cert, ctx.settings, ctx.matcher, ctx.db.cert_is_live, ctx.resolver)


def _process_upload(ctx: Ctx, f: dict) -> None:
    g, s, db = ctx.google, ctx.settings, ctx.db
    today = ctx.today().isoformat()
    if f.get("mimeType") != PDF:
        ctx.event("WARN", "Intake", f"'{f['name']}' is not a PDF – moved to Needs Review/Not PDF", ref=f["id"])
        if not ctx.dry:
            g.move_rename(f["id"], g.folder_path(s.folder("needs_review"), ["Not PDF"]))
        return

    if ctx.dry:
        try:
            units = _read_units(ctx, g.download(f["id"]), f["name"])
        except WholeUploadReview as w:
            ctx.preview.append(["UPLOAD", f["name"], "all pages", "", "", "", "Needs Review (whole file)", str(w)])
            ctx.event("WARN", "Intake", f"'{f['name']}' would go to Needs Review as a whole – {w}", ref=f["id"])
            return
        rows = []
        for u in units:
            a = _analyze(ctx, u)
            if a.ok:
                ctx.dry_papers.append({"cert": a.cert, "state": a.state, "amount": a.amount})
            dest = f"Inventory/{a.state}/{a.amount}/{a.cert}.pdf" if a.ok else "Needs Review"
            rows.append(["PAPER", f["name"], f"p{u.page_from}-{u.page_to}", a.cert, a.state, a.amount, dest,
                         "; ".join(a.issues) or "OK"])
        ctx.preview.extend(rows)
        ok = sum(1 for r in rows if r[7] == "OK")
        ctx.event("INFO", "Intake", f"'{f['name']}': {ok} paper(s) would be stocked, {len(rows) - ok} would go to Needs Review")
        return

    up = db.upload_get_or_create(f["id"], f["name"], f.get("md5Checksum", ""))
    if up["status"] in ("DONE", "DUPLICATE"):
        g.move_rename(f["id"], g.folder_path(s.folder("upload_archive"), [today]))
        return
    if up["status"] == "REVIEW":          # decided earlier (crash before the move, or ops moved it back)
        g.move_rename(f["id"], g.folder_path(s.folder("needs_review"), [today]))
        ctx.event("WARN", "Needs review", f"'{f['name']}' was already sent to Needs Review as a whole ({up['notes']}) – "
                  "moved back there. To process it after a fix, upload it again as a new file.", ref=f["id"])
        return
    twin = db.upload_same_md5_done(f.get("md5Checksum", ""), up["id"])
    if twin:
        db.upload_finish(up["id"], "DUPLICATE", 0, f"same file content as '{twin['file_name']}' (upload {twin['id']})")
        g.move_rename(f["id"], g.folder_path(s.folder("needs_review"), ["Duplicate uploads"]))
        ctx.event("WARN", "Intake", f"'{f['name']}' is an exact duplicate of '{twin['file_name']}' – not stocked again",
                  ref=f["id"])
        return

    try:
        units = _read_units(ctx, g.download(f["id"]), f["name"])
    except WholeUploadReview as w:
        db.upload_finish(up["id"], "REVIEW", 0, str(w))
        g.move_rename(f["id"], g.folder_path(s.folder("needs_review"), [today]))
        ctx.stats.uploads += 1
        ctx.event("WARN", "Needs review", f"'{f['name']}' (whole file): {w}", ref=f["id"])
        return
    done = db.pages_done(up["id"])
    todo = [u for u in units if u.page_from not in done]
    with ThreadPoolExecutor(max_workers=int(s.get("workers", 8))) as ex:
        list(ex.map(lambda u: _stock_unit(ctx, up, f["name"], u), todo))

    db.upload_finish(up["id"], "DONE", len(units))
    g.move_rename(f["id"], g.folder_path(s.folder("upload_archive"), [today]))
    ctx.stats.uploads += 1
    ctx.event("INFO", "Intake", f"'{f['name']}': {len(units)} paper(s) processed", ref=f["id"])


def _stock_unit(ctx: Ctx, up: dict, upload_name: str, u: Unit) -> None:
    g, s, db = ctx.google, ctx.settings, ctx.db
    a = _analyze(ctx, u)
    data = u.data
    review_name = f"{safe_file_name(upload_name.rsplit('.', 1)[0])}__p{u.page_from}-{u.page_to}.pdf"
    fields = dict(upload_id=up["id"], page_from=u.page_from, page_to=u.page_to, cert_no=a.cert or None,
                  cert_state=a.cert_state or None, state=a.state or None, denomination=a.amount or None,
                  ocr_amount=a.amount or None, ocr_via=u.via, ocr_text=u.text[:20000])

    if a.ok:
        folder = g.folder_path(s.folder("inventory_root"), [a.state, str(a.amount)])
        name = f"{a.cert}.pdf"
        file_id = g.upload_pdf(folder, name, data)
        try:
            db.insert_paper(**fields, drive_file_id=file_id, file_name=name, status="AVAILABLE")
            with ctx._lock:
                ctx.stats.papers_in += 1
            return
        except UniqueViolation:
            a.issues.append(f"duplicate certificate {a.cert} – already in stock")
            g.move_rename(file_id, g.folder_path(s.folder("needs_review"), [ctx.today().isoformat()]), review_name)
            db.insert_paper(**fields, drive_file_id=file_id, file_name=review_name, status="NEEDS_REVIEW",
                            notes="; ".join(a.issues))
    else:
        folder = g.folder_path(s.folder("needs_review"), [ctx.today().isoformat()])
        file_id = g.upload_pdf(folder, review_name, data)
        db.insert_paper(**fields, drive_file_id=file_id, file_name=review_name, status="NEEDS_REVIEW",
                        notes="; ".join(a.issues))
    with ctx._lock:
        ctx.stats.papers_review += 1
    ctx.event("WARN", "Needs review", f"{upload_name} pages {u.page_from}-{u.page_to}: {'; '.join(a.issues)}",
              ref=file_id)
