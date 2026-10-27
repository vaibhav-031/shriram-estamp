"""Allocation: each PENDING LAN gets the oldest AVAILABLE paper of the same state + stamp duty.

Per LAN (in parallel worker threads):
  1. reserve a paper in Postgres (FOR UPDATE SKIP LOCKED → never double-allocated)
  2. backup copy → Backup/<date>/<STATE>/<AMOUNT>/<LAN>__<CERT>.pdf
  3. rename + move in ONE Drive call → Output/<date>/<STATE>/<STATE-AMOUNT> PDFs/<LAN>.pdf
  4. VERIFY: OCR the issued <LAN>.pdf again and confirm the certificate printed on it is the one the
     ledger gave this LAN. Mismatch → the PDF is pulled into Needs Review/<date>/Cert mismatch, LAN + paper
     go to ERROR, nothing is written to the Output sheet. (allocation.verify_ocr: true/false)
  5. ledger: allocation row (with the OCR-verified cert), paper ASSIGNED, order DONE (one transaction)
The Output sheet rows are written afterwards in batches (outputs.py).

Any failure parks BOTH the LAN and the paper in ERROR for a human. Nothing is auto-reassigned.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from .constants import BACKUP_ROOT, NEEDS_REVIEW, OUTPUT_ROOT
from .context import Ctx
from .errors import line
from .extract import ids_in_reading, safe_file_name
from .google_clients import file_link
from .ocr_split import _split_local


class CertMismatch(RuntimeError):
    pass


def verify_issued(ctx: Ctx, file_id: str, expected: str, name: str) -> tuple[str, str]:
    """OCR every page of the issued file; return (id found, via). Raises CertMismatch unless it is exactly `expected`."""
    found, via = set(), ""
    for p in _split_local(ctx.google.download(file_id), 1):
        r = ctx.ocr.read(p.data, f"{name}_p{p.page_from}")
        found |= {h.cert for h in ids_in_reading(r.text, r.cert, r.fields, ctx.settings, ctx.matcher, ctx.resolver)}
        via = r.via
    if found != {expected}:
        seen = ", ".join(sorted(found)) or "no certificate / serial readable"
        raise CertMismatch(f"issued PDF shows [{seen}] but ledger expected {expected}")
    return expected, via


def label_for(state: str, denom: int) -> str:
    return f"{state}-{denom}"


def run_allocation(ctx: Ctx, deadline: float, report_shortage: bool = True) -> None:
    s, db = ctx.settings, ctx.db
    max_per_cycle = int(s.get("allocation.max_per_cycle", 3000))
    batch = int(s.get("allocation.batch_size", 200))
    done_this_cycle = 0
    short: dict[str, int] = {}

    if ctx.dry:
        if report_shortage:          # final pass only, after this dry run's intake
            _preview(ctx)
        return
    else:
        with ThreadPoolExecutor(max_workers=int(s.get("workers", 8))) as ex:
            progress = True
            while progress and done_this_cycle < max_per_cycle and time.time() < deadline:
                progress = False
                for grp in db.pending_groups():
                    if grp["avail"] <= 0:
                        continue
                    take = min(grp["pending"], grp["avail"], batch, max_per_cycle - done_this_cycle)
                    if take <= 0:
                        break
                    claimed = db.claim_orders(grp["state"], grp["denomination"], take)
                    results = list(ex.map(lambda o: _allocate_one(ctx, o), claimed))
                    n_ok = sum(1 for r in results if r == "OK")
                    done_this_cycle += n_ok
                    progress = progress or n_ok > 0
                    if time.time() > deadline:
                        ctx.event("INFO", "Allocate", "time budget reached – continuing next cycle")
                        break

    if not report_shortage:
        return
    for grp in db.pending_groups():        # what is still unmet after the final pass
        if grp["pending"] > grp["avail"]:
            short[f"{grp['state']}|{grp['denomination']}"] = grp["pending"] - grp["avail"]
    for key, n in short.items():
        st, d = key.split("|")
        ctx.stats.waiting += n
        ctx.event("WARN", "Shortage", line("E301", f"{st} ₹{d}: {n} LAN(s) waiting – not enough AVAILABLE papers. "
                  "They fulfil automatically once stock is uploaded."), alert_key=f"short|{key}")


def _preview(ctx: Ctx) -> None:
    """Dry run: show which paper each LAN WOULD get – using saved data plus what this dry run read in memory."""
    d = ctx.today().isoformat()
    pend: dict[tuple, list] = {}
    for o in ctx.db.q("SELECT * FROM orders WHERE status='PENDING' ORDER BY id"):
        pend.setdefault((o["state"], int(o["denomination"])), []).append(o)
    for o in ctx.dryPending:
        pend.setdefault((o["State"], int(o["Denomination"])), []).append(
            {"lan": o["LAN ID"], "source_file": o["Source File"], "source_tab": o["Source Tab"], "source_row": o["Source Row"]})
    avail: dict[tuple, list] = {}
    for p in ctx.db.q("SELECT cert_no, state, denomination FROM papers WHERE status='AVAILABLE' ORDER BY added_at, id"):
        avail.setdefault((p["state"], int(p["denomination"])), []).append(p["cert_no"])
    for p in ctx.dry_papers:
        avail.setdefault((p["state"], int(p["amount"])), []).append(p["cert"])
    for (st, amt), orders in pend.items():
        certs = avail.get((st, amt), [])
        for i, o in enumerate(orders):
            c = certs[i] if i < len(certs) else None
            ctx.preview.append(["ALLOCATE", o["source_file"], f"{o['source_tab']}!row {o['source_row']}", o["lan"], st, amt,
                                f"{c} → Output/{d}/{st}/{st}-{amt} PDFs/{safe_file_name(o['lan'])}.pdf" if c else "-",
                                "OK (dry run)" if c else "NO STOCK – would wait"])
    n_ok = sum(1 for r in ctx.preview if r[0] == "ALLOCATE" and r[7].startswith("OK"))
    n_all = sum(1 for r in ctx.preview if r[0] == "ALLOCATE")
    if n_all:
        ctx.event("INFO", "Allocate", f"dry run: {n_ok} of {n_all} LAN(s) would get a paper now, {n_all - n_ok} would wait")


def _allocate_one(ctx: Ctx, order: dict) -> str:
    g, s, db = ctx.google, ctx.settings, ctx.db
    paper = db.reserve_paper(order)
    if not paper:
        db.release_order(order["id"])
        return "NO_STOCK"

    d = ctx.today()
    state, denom = order["state"], order["denomination"]
    label = label_for(state, denom)
    lan_safe = safe_file_name(order["lan"])
    pdf_name = f"{lan_safe}.pdf"
    step = "prepare folders"
    try:
        out_folder = g.folder_path(s.folder(OUTPUT_ROOT), [d.isoformat(), state, f"{label} PDFs"])
        if g.child_exists(out_folder, pdf_name):
            raise RuntimeError(f"'{pdf_name}' already exists in the output folder")
        step = "backup copy"
        backup_folder = g.folder_path(s.folder(BACKUP_ROOT), [d.isoformat(), state, str(denom)])
        backup_id = g.copy(paper["drive_file_id"], backup_folder, f"{lan_safe}__{paper['cert_no']}.pdf")
        step = "rename + move"
        g.move_rename(paper["drive_file_id"], out_folder, pdf_name)
        verified, via = None, None
        if s.get("allocation.verify_ocr", True):
            step = "verify OCR on issued PDF"
            try:
                verified, via = verify_issued(ctx, paper["drive_file_id"], paper["cert_no"], pdf_name)
            except CertMismatch:
                g.move_rename(paper["drive_file_id"],
                              g.folder_path(s.folder(NEEDS_REVIEW), [d.isoformat(), "Cert mismatch"]),
                              f"{lan_safe}__expected_{paper['cert_no']}.pdf")
                raise
        step = "update ledger"
        db.finish_allocation(order, paper, d, pdf_name, paper["drive_file_id"], file_link(paper["drive_file_id"]),
                             backup_id, verified, via)
        with ctx._lock:
            ctx.stats.allocated += 1
        return "OK"
    except Exception as e:
        msg = f"failed at '{step}': {e}"
        try:
            db.fail_allocation(order, paper, msg)
        finally:
            # Only an issued-PDF cert mismatch (E302) gets a code; other failing steps stay plain text.
            details = line("E302", f"LAN {order['lan']} ({label}): {msg}") if isinstance(e, CertMismatch) \
                else f"LAN {order['lan']} ({label}): {msg}"
            ctx.event("ERROR", "Allocate", details, ref=str(order["id"]))
        return "ERROR"
