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

from .extract import (PaperAnalysis, analyze_fields, analyze_paper, first_broken_pair, ids_in_reading,
                      pages_per_paper, safe_file_name)
from .google_clients import FOLDER, PDF, parse_time
from .ocr_split import _split_local, merge_pdfs


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
    """One paper: one page (e-stamp, 1-page states) or front + back (traditional paper)."""
    page_from: int
    page_to: int
    parts: list[bytes]
    texts: list[str]
    api_cert: str = ""
    via: str = ""
    error: str = ""
    fields: dict | None = None      # OCR API reading of the paper's first page (None = text reader)
    raw: dict | None = None         # what is stored per paper: {"fields": …, "responses": {page: raw response}}

    @property
    def data(self) -> bytes:
        return merge_pdfs(self.parts)

    @property
    def text(self) -> str:
        return "\n".join(self.texts)


class WholeUploadReview(Exception):
    """The whole upload goes to Needs Review untouched: E105 type unclear / pages disagree, E106 odd page count."""

    def __init__(self, code: str, reason: str):
        super().__init__(f"{code}: {reason}")
        self.code, self.reason = code, reason


def _read_units(ctx: Ctx, data: bytes, name: str) -> list[Unit]:
    """OCR every page → pages per paper from the IDs (ADR-0006) → split → papers.

    The first page with exactly one ID decides the file: an e-stamp (IN-… / SUBIN-…) → 1 page per paper; any other
    ID → its state's pages_per_paper (state_config.csv, default 2 = front + back). Every other page with an ID must
    imply the same, else E105 (whole file). No page with an ID → E105. 2-page papers and an odd page count → E106
    (before the split API is called – it would not object). 2-page papers: the first pair whose front doesn't carry
    exactly one ID, or whose back carries one, breaks the pairing → that pair and every later pair to Needs Review
    (E107) – one missing page shifts every pair after it."""
    s = ctx.settings
    singles = _split_local(data, 1)
    if not singles:
        raise RuntimeError("PDF has no pages")
    ctx.event("INFO", "Intake", f"'{name}': {len(singles)} page(s) – reading them now")
    done = [0]

    def read(no):
        try:
            return no, (ctx.ocr.read(singles[no - 1].data, f"{name}_p{no}"), "")
        except Exception as e:
            return no, (None, f"OCR failed on page {no}: {e}")
        finally:
            with ctx._lock:
                done[0] += 1
                if done[0] % 10 == 0 or done[0] == len(singles):
                    log.info(f"Intake: '{name}': read {done[0]}/{len(singles)}")

    with ThreadPoolExecutor(max_workers=int(s.get("workers", 8))) as ex:
        reads = dict(ex.map(read, range(1, len(singles) + 1)))

    def ids(no):
        r, e = reads[no]
        return [] if e else ids_in_reading(r.text, r.cert, r.fields, s, ctx.matcher, ctx.resolver)

    page_ids = {no: ids(no) for no in reads}
    deciding = next((no for no in sorted(reads) if len(page_ids[no]) == 1), None)
    if deciding is None:
        first_err = next((e for r, e in reads.values() if e), "")
        if first_err:       # can't tell the type while pages are unreadable – retry the whole file next cycle
            raise RuntimeError(f"{first_err} – the paper type can't be decided, retried next cycle")
        raise WholeUploadReview("E105", "no page has a readable certificate / serial, so 1- or 2-page papers can't "
                                        "be told apart. Check the file")
    if any(e for no, (r, e) in reads.items() if no < deciding):
        raise RuntimeError(f"{reads[min(no for no, (r, e) in reads.items() if e)][1]} – retried next cycle")
    size = pages_per_paper(page_ids[deciding][0], s)
    clash = [(no, h.cert) for no in sorted(reads) for h in page_ids[no] if pages_per_paper(h, s) != size]
    if clash:
        no, cert = clash[0]
        raise WholeUploadReview("E105", f"papers of different kinds in one file – page {deciding} is a "
                                        f"{size}-page paper, page {no} ({cert}) is not. Split the file by kind and "
                                        "upload again")
    if size == 2 and len(singles) % 2:
        raise WholeUploadReview("E106", f"{len(singles)} pages (odd) – every paper here needs front + back. "
                                        "Find the missing page and upload again")

    pieces = ctx.splitter.split(data, name, size)
    wrong = [p for p in pieces if p.page_to - p.page_from + 1 != size]
    if wrong:   # never stock a part that holds more (or less) than one paper – retried next cycle
        raise RuntimeError(f"split returned parts that are not {size} page(s) each "
                           f"(e.g. pages {wrong[0].page_from}-{wrong[0].page_to}) – nothing stocked")

    def unit(p):
        pages = range(p.page_from, p.page_to + 1)
        first, err = reads[p.page_from]
        texts = [reads[no][0].text if reads[no][0] else "" for no in pages]
        error = next((reads[no][1] for no in pages if reads[no][1]), "")
        raw = None
        if first is not None and first.fields is not None:
            raw = {"fields": first.fields, "responses": {str(no): reads[no][0].raw for no in pages if reads[no][0]}}
        return Unit(p.page_from, p.page_to, [p.data], texts, first.cert if first else "",
                    first.via if first else "error", error, first.fields if first else None, raw)

    units = [unit(p) for p in pieces]
    if size == 2:
        # A pair whose OCR failed can't prove or disprove the pairing; it goes to review on its own error.
        counts = [(1, 0) if u.error else (len(page_ids[u.page_from]), len(page_ids[u.page_to])) for u in units]
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
    if u.fields is not None:
        return analyze_fields(u.fields, ctx.settings, ctx.matcher, ctx.db.cert_is_live, ctx.resolver)
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
                  ocr_amount=a.amount or None, ocr_via=u.via, ocr_text=u.text[:20000], ocr_raw=u.raw)

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
