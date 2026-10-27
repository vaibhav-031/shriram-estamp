"""Write allocations to the Shriram-facing sheets: Output/<date>/<STATE>/<STATE-AMOUNT> (one per day/state/amount).

Rows are appended in batches (one API call per sheet) and flagged in the ledger only after the write
succeeds, so a failed write is simply retried next cycle.
"""
from __future__ import annotations

import threading
from itertools import groupby

from .allocate import label_for
from .context import Ctx

OUTPUT_HEADER = ["LAN ID", "Certificate No", "PDF Name", "PDF Link", "Processed At", "Status"]
_sheet_lock = threading.Lock()


def output_sheet_id(ctx: Ctx, d, state: str, denom: int) -> str:
    label = label_for(state, denom)
    with _sheet_lock:
        sid = ctx.db.output_sheet(d, state, label)
        if sid:
            return sid
        folder = ctx.google.folder_path(ctx.settings.folder("output_root"), [d.isoformat(), state])
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
            sid = output_sheet_id(ctx, d, state, denom)
            ctx.google.append_rows(sid, label_for(state, denom), [
                [r["lan"], r["verified_cert"] or r["cert_no"], r["pdf_name"], r["pdf_link"],
                 r["created_at"].astimezone(ctx.tz).strftime("%Y-%m-%d %H:%M:%S"), "ISSUED"] for r in grp])
            ctx.db.mark_written([r["id"] for r in grp])
            ctx.stats.outputs_written += len(grp)
        except Exception as e:
            ctx.event("ERROR", "Output sheet", f"{d} {state}-{denom}: {e} – will retry next cycle",
                      alert_key=f"outfail|{d}|{state}|{denom}")
