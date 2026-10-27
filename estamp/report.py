"""Ops-manager report (D-2) and vendor reconciliation (D-3).

`python -m estamp report --date YYYY-MM-DD` (or --from/--to) builds a Google Sheet under the
reconciliation_root folder, in a DATE subfolder, with two tabs, both read straight from the ledger:

  * Daily Report        – one row per (state, stamp duty) issued in the range + a Total row.
  * Vendor Reconciliation – per (vendor × state × stamp duty) paper counts, an editable rate column,
    and payable/disputed/net-payable formulas + a balance-check row.

Everything is a sheet FORMULA (Amount, totals, payable…) so ops edits recalc live – nothing is a typed
number. The report is read-only against the ledger. Re-running a date reuses the one Daily Report sheet
(cleared + rewritten), never a second one (no file is ever deleted – A-09).
"""
from __future__ import annotations

from datetime import date

from .constants import RECONCILIATION_ROOT
from .context import Ctx
from .errors import line
from .google_clients import file_link

SHEET_NAME = "Daily Report"
RECON_TAB = "Vendor Reconciliation"

REPORT_HEADER = ["State", "Stamp Duty (₹)", "Quantity", "Amount (₹)"]
RECON_HEADER = ["Vendor", "State", "Stamp Duty (₹)", "Received", "Issued", "In Stock", "In Review",
                "Rejected/Dup", "Rate per paper (₹)", "Payable (received × rate)", "Disputed (rejected × rate)",
                "Net payable"]


def report_grid(ctx: Ctx, d_from: date, d_to: date) -> list[list]:
    """The Daily Report rows (header + one per state/amount + Total), Amount and totals as formulas."""
    issued = ctx.db.issued_by_state_amount(d_from, d_to)
    rows: list[list] = [REPORT_HEADER]
    for r in issued:
        n = len(rows) + 1                                   # sheet row this becomes (1-based, header is row 1)
        rows.append([r["state"], r["denomination"], r["qty"], f"=B{n}*C{n}"])  # Amount = Stamp Duty × Quantity
    last = len(rows)                                        # last data row; header is row 1, data from row 2
    rows.append(["Total", "", _sum("C", last), _sum("D", last)])
    return rows


def _sum(col: str, last_data_row: int) -> str:
    """=SUM(<col>2:<col>last) – empty range (no data) sums to 0, which is correct for an empty report."""
    return f"=SUM({col}2:{col}{max(last_data_row, 2)})"


def _recon_grid(ctx: Ctx, d_from: date, d_to: date) -> list[list]:
    """Vendor reconciliation rows. Emits E402 for any group whose received count doesn't equal the sum of the
    five buckets (issued + in stock + in review + rejected)."""
    groups = ctx.db.papers_by_vendor()
    rows: list[list] = [RECON_HEADER]
    for r in groups:
        n = len(rows) + 1
        bucketed = r["issued"] + r["in_stock"] + r["in_review"] + r["rejected"]
        if r["received"] != bucketed:
            ctx.event("ERROR", "Reconcile", line("E402",
                      f"{r['vendor_code']} {r['state']} ₹{r['denomination']}: received {r['received']} "
                      f"≠ issued+stock+review+rejected ({bucketed})"),
                      alert_key=f"recon|{r['vendor_code']}|{r['state']}|{r['denomination']}", dedup="day")
        rows.append([r["vendor_code"], r["state"], r["denomination"], r["received"], r["issued"],
                     r["in_stock"], r["in_review"], r["rejected"], "",      # I = editable rate (ops type it)
                     f"=D{n}*I{n}", f"=H{n}*I{n}", f"=J{n}-K{n}"])           # Payable / Disputed / Net payable
    last = len(rows)                                       # last data row; header is row 1, data from row 2
    # Balance-check row: each bucket column summed. received (D) should equal issued+stock+review+rejected
    # (E..H) – the per-group E402 above is the authoritative flag; this row lets ops eyeball the totals.
    rows.append(["Balance check (received = issued + stock + review + rejected)", "", "",
                 _sum("D", last), _sum("E", last), _sum("F", last), _sum("G", last), _sum("H", last),
                 "", _sum("J", last), _sum("K", last), _sum("L", last)])
    return rows


def _report_sheet_id(ctx: Ctx, d_from: date) -> tuple[str, bool]:
    """(spreadsheet_id, reused). The sheet lives in reconciliation_root/<from-date>/. Reuse the existing
    'Daily Report' there (clear + rewrite) so re-running replaces rather than piling up a second file."""
    folder = ctx.google.folder_path(ctx.settings.folder(RECONCILIATION_ROOT), [d_from.isoformat()])
    existing = ctx.google.child_id(folder, SHEET_NAME)
    if existing:
        return existing, True
    return ctx.google.create_sheet(folder, SHEET_NAME, REPORT_HEADER), False


def run_report(ctx: Ctx, d_from: date, d_to: date) -> str:
    """Build (or replace) the Daily Report sheet for the date range; returns its spreadsheet id."""
    sid, reused = _report_sheet_id(ctx, d_from)
    span = d_from.isoformat() if d_from == d_to else f"{d_from.isoformat()}..{d_to.isoformat()}"
    ctx.google.ensure_tabs(sid, {SHEET_NAME: REPORT_HEADER, RECON_TAB: RECON_HEADER})
    ctx.google.overwrite_tab(sid, SHEET_NAME, report_grid(ctx, d_from, d_to), value_input="USER_ENTERED")
    ctx.google.overwrite_tab(sid, RECON_TAB, _recon_grid(ctx, d_from, d_to), value_input="USER_ENTERED")
    verb = "Replaced" if reused else "Built"
    ctx.event("INFO", "Report", f"{verb} Daily Report for {span}: {file_link(sid)}")
    return sid
