"""Order ingestion: Shriram's state-wise LAN sheets (Google Sheet / .xlsx / .csv) in Orders Incoming.

Per row: LAN format, state (column → tab name → file name), stamp duty (column → state default),
state active, amount allowed for the state, duplicates in file, duplicates in the ledger.

Same LAN arriving for a DIFFERENT state while still unfulfilled → both copies go to CONFLICT and nobody
gets a paper until ops picks the right state (Order Actions tab → KEEP_STATE).
"""
from __future__ import annotations

import csv
import io
import time
from datetime import datetime, timezone

from .constants import ORDERS_INCOMING, ORDERS_PROCESSED, ORDERS_REJECTED
from .context import Ctx
from .errors import line
from .extract import ParsedOrderFile, parse_order_tabs
from .google_clients import CSV, GSHEET, XLS, XLSX, parse_time


def run_ingest(ctx: Ctx, deadline: float) -> None:
    g, s = ctx.google, ctx.settings
    settle = int(s.get("orders.settle_minutes", 15)) * 60
    for f in g.list_children(s.folder(ORDERS_INCOMING), folders=False):
        if time.time() > deadline:
            return
        age = (datetime.now(timezone.utc) - parse_time(f["modifiedTime"])).total_seconds()
        if age < settle:
            continue
        try:
            _ingest_file(ctx, f)
        except Exception as e:
            ctx.stats.files_rejected += 1
            ctx.event("ERROR", "Orders", line("E201", f"'{f['name']}' rejected: {e}"), ref=f["id"])
            if not ctx.dry:
                g.move_rename(f["id"], s.folder(ORDERS_REJECTED))


def _read_tabs(ctx: Ctx, f: dict) -> list[tuple[str, list[list[str]]]]:
    g, mt = ctx.google, f.get("mimeType")
    if mt == GSHEET:
        return g.read_tabs(f["id"])
    data = g.download(f["id"])
    if mt == CSV or f["name"].lower().endswith(".csv"):
        return [(f["name"], list(csv.reader(io.StringIO(data.decode("utf-8-sig", "replace")))))]
    if mt in (XLSX, XLS) or f["name"].lower().endswith((".xlsx", ".xls")):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets:
            rows = []
            for r in ws.iter_rows(values_only=True):
                rows.append(["" if v is None else (str(int(v)) if isinstance(v, float) and v.is_integer() else str(v))
                             for v in r])
            out.append((ws.title, rows))
        return out
    raise ValueError(f"unsupported file type {mt} – send a Google Sheet, .xlsx or .csv")


def _ingest_file(ctx: Ctx, f: dict) -> None:
    g, s, db = ctx.google, ctx.settings, ctx.db
    parsed: ParsedOrderFile = parse_order_tabs(_read_tabs(ctx, f), f["name"], s, ctx.resolver)
    if not parsed.header_found:
        raise ValueError("no header row with a LAN column in any tab (check orders.columns in config.yaml)")
    if parsed.invalid and s.get("orders.reject_file_on_any_invalid_row", False):
        raise ValueError(f"{len(parsed.invalid)} invalid row(s): " + " | ".join(parsed.invalid[:15]))

    existing = db.existing_orders([o.lan_key for o in parsed.valid])
    to_insert, dups, conflicts = [], list(parsed.duplicates), []
    for o in parsed.valid:
        prev = existing.get(o.lan_key, [])
        base = (o.lan, o.lan_key, o.state, o.denomination, f["name"], f["id"], o.tab, o.row_no)
        if not prev:
            to_insert.append(base + ("PENDING", None))
        elif all(p["state"] == o.state for p in prev):
            dups.append(f"{o.tab}!row {o.row_no}: {o.lan} already received (status {prev[-1]['status']})")
        elif any(p["status"] in ("DONE", "ALLOCATING") for p in prev):
            dups.append(f"{o.tab}!row {o.row_no}: {o.lan} for {o.state} was ALREADY FULFILLED for "
                        f"{[p['state'] for p in prev]} – not processed")
        else:
            note = f"LAN received for several states: {sorted({p['state'] for p in prev} | {o.state})} – pick one"
            if not ctx.dry:
                db.mark_conflict([p["id"] for p in prev], note)
            to_insert.append(base + ("CONFLICT", note))
            conflicts.append(f"{o.lan} ({note})")

    if not to_insert and not parsed.valid:
        raise ValueError(f"no valid rows ({len(parsed.invalid)} invalid, {len(dups)} duplicates): "
                         + " | ".join((parsed.invalid + dups)[:15]))

    if ctx.dry:
        for r in to_insert:
            ctx.preview.append(["ORDER", f["name"], f"{r[6]}!row {r[7]}", r[0], r[2], r[3], r[8], r[9] or "OK"])
            if r[8] == "PENDING":
                ctx.dryPending.append({"LAN ID": r[0], "State": r[2], "Denomination": r[3], "Source File": r[4],
                                       "Source Tab": r[6], "Source Row": r[7]})
        for m in parsed.invalid + dups:
            ctx.preview.append(["ORDER", f["name"], "", "", "", "", "SKIPPED", m])
    else:
        db.insert_orders(to_insert)
        g.move_rename(f["id"], s.folder(ORDERS_PROCESSED))

    ctx.stats.orders_in += sum(1 for r in to_insert if r[8] == "PENDING")
    ctx.stats.rows_rejected += len(parsed.invalid)
    ctx.stats.duplicates += len(dups)
    ctx.stats.conflicts += len(conflicts)
    ctx.event("INFO", "Orders", f"'{f['name']}': {sum(1 for r in to_insert if r[8] == 'PENDING')} LAN(s) queued",
              ref=f["id"])
    if parsed.invalid:
        ctx.event("WARN", "Orders", line("E203", f"'{f['name']}': {len(parsed.invalid)} row(s) skipped – "
                  + " | ".join(parsed.invalid[:20])))
    if dups:
        ctx.event("WARN", "Orders", line("E205", f"'{f['name']}': {len(dups)} duplicate(s) – "
                  + " | ".join(dups[:20])))
    if conflicts:
        ctx.event("WARN", "Orders", line("E206", f"'{f['name']}': {len(conflicts)} LAN(s) in CONFLICT – resolve in "
                  "Order Actions: " + " | ".join(conflicts[:20])))
