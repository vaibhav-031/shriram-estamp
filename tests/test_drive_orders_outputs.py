"""Orders / outputs / Drive client / actions hardening (AUDIT H-3, L-2, M-1, M-5 + the dead E105 recheck skip)."""
from __future__ import annotations

import io
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from estamp.google_clients import GSHEET, PDF, XLSX, Google

import samples  # noqa: E402
from fake_google import FakeGoogle  # noqa: E402
from test_all import _ctx, _vendor_folder, e2e, env, settings  # noqa: F401,E402  (fixtures)


def _xlsx(rows: list[list]) -> bytes:
    """A real .xlsx built with openpyxl. A cell given as '=…' is saved as a formula WITHOUT a cached value –
    exactly what LibreOffice-headless / some exporters produce."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "MH"
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _reader(settings, g):
    return SimpleNamespace(google=g, settings=settings)


# ============================================================ H-3: formulas without cached values, row cap

def test_xlsx_formula_column_without_cached_values_is_refused_with_a_clear_reason(settings):
    from estamp.orders import _read_tabs
    g = FakeGoogle()
    rows = [["LAN ID", "State"]] + [[f'=CONCAT("LAN", "{i:05d}")', "MH"] for i in range(5)]
    fid = g.add("formulas.xlsx", XLSX, None, _xlsx(rows))
    with pytest.raises(ValueError, match="formulas with no cached values"):
        _read_tabs(_reader(settings, g), g.get_file(fid))


def test_plain_xlsx_still_reads(settings):
    from estamp.orders import _read_tabs
    g = FakeGoogle()
    fid = g.add("plain.xlsx", XLSX, None, _xlsx([["LAN ID", "State"], ["LAN00001", "MH"], [12345.0, "MH"]]))
    assert _read_tabs(_reader(settings, g), g.get_file(fid)) == [
        ("MH", [["LAN ID", "State"], ["LAN00001", "MH"], ["12345", "MH"]])]


@pytest.mark.parametrize("kind", ["xlsx", "csv", "gsheet"])
def test_a_tab_over_the_row_cap_is_refused(settings, monkeypatch, kind):
    from estamp.orders import _read_tabs
    monkeypatch.setitem(settings.raw["orders"], "max_rows_per_tab", 5)
    g = FakeGoogle()
    rows = [["LAN ID", "State"]] + [[f"LAN{i:05d}", "MH"] for i in range(10)]
    if kind == "xlsx":
        fid = g.add("big.xlsx", XLSX, None, _xlsx(rows))
    elif kind == "csv":
        fid = g.add("big.csv", "text/csv", None, "\n".join(",".join(r) for r in rows).encode())
    else:
        fid = g.add("big", GSHEET, None, tabs={"MH": rows})
    with pytest.raises(ValueError, match=r"more than 5 rows.*orders\.max_rows_per_tab"):
        _read_tabs(_reader(settings, g), g.get_file(fid))


def test_row_cap_counts_only_filled_rows(settings, monkeypatch):
    # Blank formatted rows at the end of a sheet are not orders – they must not trip the cap or be kept.
    from estamp.orders import _read_tabs
    monkeypatch.setitem(settings.raw["orders"], "max_rows_per_tab", 3)
    g = FakeGoogle()
    rows = [["LAN ID", "State"], ["LAN00001", "MH"], [None, None], ["LAN00002", "MH"]] + [[None, None]] * 20
    fid = g.add("gaps.xlsx", XLSX, None, _xlsx(rows))
    (title, got), = _read_tabs(_reader(settings, g), g.get_file(fid))
    assert [r for r in got if any(r)] == [["LAN ID", "State"], ["LAN00001", "MH"], ["LAN00002", "MH"]]
    assert len(got) == 4                     # interior blank kept (row numbers stay right), trailing ones dropped


@e2e
def test_formula_file_is_rejected_with_the_reason_not_lan_blank(env):
    from estamp.orders import run_ingest
    s, db, g = env
    rows = [["LAN ID", "State"]] + [[f'=CONCAT("LAN", "{i:05d}")', "MH"] for i in range(3)]
    fid = g.add("formulas.xlsx", XLSX, s.folder("orders_incoming"), _xlsx(rows))
    run_ingest(_ctx(s, db, g), time.time() + 60)
    assert g.files[fid]["parents"] == [s.folder("orders_rejected")]
    ev = db.q1("SELECT details FROM events WHERE action='Orders' AND level='ERROR'")["details"]
    assert "E201" in ev and "formulas with no cached values" in ev and "LAN blank" not in ev, ev


# ============================================================ L-2: Drive item without modifiedTime

@e2e
def test_order_file_without_modified_time_waits_out_the_settle_window(env, monkeypatch):
    from estamp import orders
    s, db, g = env
    monkeypatch.setitem(s.raw["orders"], "settle_minutes", 15)
    fid = g.add("TEST_Shriram_MH_LANs.csv", "text/csv", s.folder("orders_incoming"), b"LAN ID,State\nLAN00001,MH\n")
    del g.files[fid]["modifiedTime"]
    ctx = _ctx(s, db, g)
    orders.run_ingest(ctx, time.time() + 60)            # first sighting: treated as "just modified" → waits
    assert g.files[fid]["parents"] == [s.folder("orders_incoming")]
    assert ctx.stats.files_rejected == 0
    orders.run_ingest(ctx, time.time() + 60)            # still inside the window, still waits (no rejection)
    assert g.files[fid]["parents"] == [s.folder("orders_incoming")]
    orders._first_seen[fid] -= timedelta(minutes=16)    # the settle window has passed since we first saw it
    orders.run_ingest(ctx, time.time() + 60)
    assert g.files[fid]["parents"] == [s.folder("orders_processed")]
    assert db.q1("SELECT count(*) n FROM orders")["n"] == 1


# ============================================================ recheck_review: E107 papers stay put

@e2e
def test_recheck_review_leaves_e107_pairing_papers_for_a_reupload(env):
    """E107 papers read fine on their own (F3+B3 is a valid pair) – recheck must still not stock them: their
    pairing is suspect, so only a re-upload can clear them."""
    from estamp.actions import recheck_review
    from estamp.cycle import run_cycle
    s, db, g = env
    f = lambda n: samples.scanned_front("RAJASTHAN", f"S 25420{n}", 195)
    b = lambda: samples.scanned_back("RAJASTHAN")
    g.add("broken.pdf", PDF, _vendor_folder(g, s), samples.merged([f(1), b(), f(2), f(3), b(), f(4)]))
    assert run_cycle(_ctx(s, db, g))
    before = db.q("SELECT id, notes FROM papers WHERE status='NEEDS_REVIEW' ORDER BY id")
    # Intake stamps E120 in front (no cert on the unit), so the E107 marker is NOT at the start of the note.
    assert len(before) == 2 and all("E107 pairing broken" in r["notes"] for r in before), before
    assert recheck_review(_ctx(s, db, g)) == (0, 2)
    assert db.q("SELECT id, notes FROM papers WHERE status='NEEDS_REVIEW' ORDER BY id") == before
