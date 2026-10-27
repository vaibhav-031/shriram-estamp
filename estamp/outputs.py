"""Write allocations to the Shriram-facing sheets: Output/<date>/<STATE>/<STATE-AMOUNT> (one per day/state/amount).

Rows are appended in batches (one API call per sheet) and flagged in the ledger only after the write
succeeds, so a failed write is simply retried next cycle.
"""
from __future__ import annotations

import threading
from itertools import groupby

from .allocate import label_for
from .constants import OUTPUT_ROOT
from .context import Ctx

OUTPUT_HEADER = ["LAN ID", "Certificate No", "PDF Name", "PDF Link", "Processed At", "Status"]
_sheet_lock = threading.Lock()


def output_sheet_id(ctx: Ctx, d, state: str, denom: int) -> str:
    label = label_for(state, denom)
    with _sheet_lock:
        sid = ctx.db.output_sheet(d, state, label)
        if sid:
            return sid
        folder = ctx.google.folder_path(ctx.settings.folder(OUTPUT_ROOT), [d.isoformat(), state])
        sid = ctx.google.create_sheet(folder, label, OUTPUT_HEADER)
        ctx.db.save_output_sheet(d, state, label, sid)
        return sid


def run_outputs(ctx: Ctx) -> None:
    if ctx.dry:
        return
    rows = ctx.db.unwritten_allocations()
    for (d, state, denom), grp in groupby(rows, key=lambda r: (r["alloc_date"], r["state"], r["denomination"])):
        grp = list(grp)
        try:
            label = label_for(state, denom)
            sid = output_sheet_id(ctx, d, state, denom)
            # By-(LAN, cert) guard (E-01 + C-1): a crash after append_rows but before mark_written leaves the row
            # in the sheet yet the allocation unwritten, so the next cycle re-fetches it. Columns A/B hold the LAN
            # and its certificate; keying on BOTH means a re-run skips the exact same issued row, while a
            # legitimately reissued cert for a LAN (after a CANCEL) still appears. Dedupes within the batch too.
            present = {(row[0], row[1]) for row in ctx.google.read_range(sid, f"{label}!A2:B") if len(row) >= 2}
            to_write, seen = [], set(present)
            for r in grp:
                cert = r["verified_cert"] or r["cert_no"]
                if (r["lan"], cert) in seen:
                    continue
                seen.add((r["lan"], cert))
                to_write.append([r["lan"], cert, r["pdf_name"], r["pdf_link"],
                                 r["created_at"].astimezone(ctx.tz).strftime("%Y-%m-%d %H:%M:%S"), "ISSUED"])
            if to_write:
                ctx.google.append_rows(sid, label, to_write)
            skipped = len(grp) - len(to_write)
            if skipped:
                ctx.event("INFO", "Output sheet", f"{d} {state}-{denom}: {skipped} LAN(s) already in sheet, not re-appended")
            # Flag the whole batch – the already-present ones too, so they aren't retried forever.
            ctx.db.mark_written([r["id"] for r in grp])
            ctx.stats.outputs_written += len(to_write)
        except Exception as e:
            ctx.event("ERROR", "Output sheet", f"{d} {state}-{denom}: {e} – will retry next cycle",
                      alert_key=f"outfail|{d}|{state}|{denom}")
