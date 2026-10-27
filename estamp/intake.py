"""Intake: ops drop vendor PDFs (merged or single) into the Upload Inbox. For each paper:

  split → OCR → find cert no + state (from cert) + stamp duty → checks →
    OK   → Inventory/<STATE>/<AMOUNT>/<CERT>.pdf     (folders created automatically)
    FAIL → Needs Review/<date>/<upload>__p<page>.pdf  (reason in the ledger + dashboard)

The original upload is never deleted: it moves to Upload Archive/<date>/.
Re-running is safe: pages already in the ledger are skipped.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .context import Ctx, log
from .db import UniqueViolation
from dataclasses import dataclass

from .extract import PaperAnalysis, analyze_paper, identify, normalize_ocr, safe_file_name
from .google_clients import FOLDER, PDF, parse_time
from .ocr_split import merge_pdfs, read_paper, split_pdf


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


def _read_units(ctx: Ctx, data: bytes, name: str) -> list[Unit]:
    """Split → OCR every piece (parallel) → group pages into papers.

    Grouping rule: a page carrying a certificate/serial starts a new paper. A page WITHOUT one that directly
    follows the front page of a PHYSICAL paper is that paper's back side. E-stamps are always one page.
    Anything else stands alone (and will go to Needs Review with the reason)."""
    s, g = ctx.settings, ctx.google
    pieces = split_pdf(data, name, s)
    ctx.event("INFO", "Intake", f"'{name}': {len(pieces)} page(s)/piece(s) – reading them now")
    done = [0]

    def read(p):
        try:
            r = read_paper(p.data, f"{name}_p{p.page_from}", s, g)
            out = (r.text, r.cert, r.via, "")
        except Exception as e:
            out = ("", "", "error", f"OCR failed: {e}")
        with ctx._lock:
            done[0] += 1
            if done[0] % 10 == 0 or done[0] == len(pieces):
                log.info(f"Intake: '{name}': read {done[0]}/{len(pieces)}")
        return out

    with ThreadPoolExecutor(max_workers=int(s.get("workers", 8))) as ex:
        results = list(ex.map(read, pieces))

    if s.get("intake.split_provider", "local") == "zoop_api":   # API already grouped pages into papers
        return [Unit(p.page_from, p.page_to, [p.data], [t], c, v, e) for p, (t, c, v, e) in zip(pieces, results)]

    units: list[Unit] = []
    last_kind = None
    estamp_markers = [m.upper() for m in s.get("intake.estamp_markers") or []]
    for p, (t, c, v, e) in zip(pieces, results):
        ids = identify(normalize_ocr(t + ("\n" + c if c else "")), s, ctx.matcher, ctx.resolver) if not e else []
        # An e-stamp is always exactly one page: never a back side, never the front of a 2-page paper –
        # even when its certificate couldn't be read (it then goes to Needs Review on its own).
        estamp = not e and any(m in t.upper() for m in estamp_markers)
        if ids:
            units.append(Unit(p.page_from, p.page_to, [p.data], [t], c, v, e))
            last_kind = ids[0].kind
        elif units and last_kind == "physical" and len(units[-1].parts) == 1 and not e and not estamp:
            u = units[-1]                                   # back side of the physical paper before it
            u.parts.append(p.data); u.texts.append(t); u.page_to = p.page_to
            last_kind = None
        else:
            units.append(Unit(p.page_from, p.page_to, [p.data], [t], c, v, e))
            # A physical FRONT whose serial couldn't be read still keeps its back page attached,
            # so ops review one 2-page paper instead of two loose pages.
            up_t = t.upper()
            markers = s.get("intake.physical_front_markers") or []
            last_kind = "physical" if not e and not estamp and any(m.upper() in up_t for m in markers) else None
    ctx.event("INFO", "Intake", f"'{name}': {len(pieces)} page(s) grouped into {len(units)} paper(s)")
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
        units = _read_units(ctx, g.download(f["id"]), f["name"])
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
    twin = db.upload_same_md5_done(f.get("md5Checksum", ""), up["id"])
    if twin:
        db.upload_finish(up["id"], "DUPLICATE", 0, f"same file content as '{twin['file_name']}' (upload {twin['id']})")
        g.move_rename(f["id"], g.folder_path(s.folder("needs_review"), ["Duplicate uploads"]))
        ctx.event("WARN", "Intake", f"'{f['name']}' is an exact duplicate of '{twin['file_name']}' – not stocked again",
                  ref=f["id"])
        return

    units = _read_units(ctx, g.download(f["id"]), f["name"])
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
