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


# ============================================================ M-1: create_sheet reuses an existing sheet

@e2e
def test_output_sheet_already_in_drive_is_reused_not_duplicated(env):
    # Another run (or a crash between create_sheet and save_output_sheet) already made the sheet in Drive,
    # but this ledger never recorded it: the next create must land on that same sheet.
    from estamp.outputs import OUTPUT_HEADER, output_sheet_id
    s, db, g = env
    d = date(2026, 10, 29)
    folder = g.folder_path(s.folder("output_root"), [d.isoformat(), "MH"])
    first = g.create_sheet(folder, "MH-500", OUTPUT_HEADER)
    sid = output_sheet_id(_ctx(s, db, g), d, "MH", 500)
    assert sid == first
    assert [f["name"] for f in g.children(folder)] == ["MH-500"]


class _StubDrive:
    """Just enough of the Drive v3 files() surface for Google.create_sheet / move_rename, no network."""

    def __init__(self, files, hide_until_created=False, slow_get=0.0):
        self.files_ = files                # id -> {id, name, mimeType, parents, createdTime}
        self.hide = hide_until_created     # first list call sees nothing (a racing create not yet visible)
        self.slow_get = slow_get
        self.updates = []
        self.lock = threading.Lock()

    def files(self):
        return self

    def _exec(self, value):
        return SimpleNamespace(execute=lambda num_retries=0: value() if callable(value) else value)

    def list(self, q, orderBy=None, **kw):
        parent = re.search(r"'([^']+)' in parents", q).group(1)
        name = re.search(r"name='([^']*)'", q).group(1)
        if self.hide:
            self.hide = False
            return self._exec({"files": []})
        hits = sorted((f for f in self.files_.values() if parent in f["parents"] and f["name"] == name
                       and f["mimeType"] == GSHEET), key=lambda f: f["createdTime"])
        return self._exec({"files": [{"id": f["id"]} for f in hits]})

    def create(self, body, **kw):
        fid = f"new{len(self.files_)}"
        self.files_[fid] = dict(id=fid, name=body["name"], mimeType=body["mimeType"], parents=body["parents"],
                                createdTime=datetime.now(timezone.utc).isoformat())
        return self._exec({"id": fid})

    def get(self, fileId, **kw):
        def value():
            parents = list(self.files_[fileId]["parents"])
            time.sleep(self.slow_get)      # widen the read-then-update window
            return {"id": fileId, "parents": parents}
        return self._exec(value)

    def update(self, fileId, body, addParents=None, removeParents="", **kw):
        def value():
            with self.lock:
                f = self.files_[fileId]
                f["parents"] = [p for p in f["parents"] if p not in removeParents.split(",")]
                if addParents and addParents not in f["parents"]:
                    f["parents"].append(addParents)
                if body.get("name"):
                    f["name"] = body["name"]
                self.updates.append((fileId, body, addParents, removeParents))
            return {"id": fileId}
        return self._exec(value)


class _StubSheets:
    """Sheets surface: `tabs` maps a spreadsheet id to (tab titles, A1 rows). Unknown ids are fully set up."""

    def __init__(self, tabs=None):
        self.calls = []
        self.tabs = tabs or {}

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def get(self, spreadsheetId, range=None, **kw):
        titles, a1 = self.tabs.get(spreadsheetId, (["MH-500"], [["LAN ID"]]))
        if range is None:      # spreadsheets().get → tab titles
            return SimpleNamespace(execute=lambda num_retries=0: {"sheets": [{"properties": {"title": t}} for t in titles]})
        return SimpleNamespace(execute=lambda num_retries=0: {"values": a1})

    def batchUpdate(self, **kw):
        self.calls.append(("batchUpdate", kw))
        return SimpleNamespace(execute=lambda num_retries=0: {})

    def update(self, **kw):
        self.calls.append(("update", kw))
        return SimpleNamespace(execute=lambda num_retries=0: {})


def _google(settings, monkeypatch, drive, sheets=None) -> Google:
    monkeypatch.setattr(Google, "account_email", lambda self: str(settings.get("google.account") or ""))
    sheets = sheets or _StubSheets()
    monkeypatch.setattr(Google, "drive", property(lambda self: drive))     # every thread, not just this one
    monkeypatch.setattr(Google, "sheets", property(lambda self: sheets))
    return Google(settings, credentials=object())


def test_create_sheet_reuses_an_existing_sheet_of_that_name(settings, monkeypatch):
    drive = _StubDrive({"old": dict(id="old", name="MH-500", mimeType=GSHEET, parents=["fold"],
                                    createdTime="2026-10-29T00:00:00+00:00")})
    sheets = _StubSheets()
    g = _google(settings, monkeypatch, drive, sheets)
    assert g.create_sheet("fold", "MH-500", ["LAN ID"]) == "old"
    assert set(drive.files_) == {"old"} and sheets.calls == []   # nothing created, header not rewritten


def test_reused_sheet_left_half_made_by_a_crash_is_finished(settings, monkeypatch):
    # A crash between files.create and the tab rename / header write leaves "Sheet1" with nothing in it. Reusing it
    # as-is would make every output append fail on the missing tab – so finish setting it up.
    drive = _StubDrive({"old": dict(id="old", name="MH-500", mimeType=GSHEET, parents=["fold"],
                                    createdTime="2026-10-29T00:00:00+00:00")})
    sheets = _StubSheets({"old": (["Sheet1"], [])})
    g = _google(settings, monkeypatch, drive, sheets)
    assert g.create_sheet("fold", "MH-500", ["LAN ID"]) == "old"
    assert [c[0] for c in sheets.calls] == ["batchUpdate", "update"]
    assert sheets.calls[1][1]["body"] == {"values": [["LAN ID"]]}


def test_racing_create_sheet_converges_on_the_oldest_and_marks_its_own_copy_unused(settings, monkeypatch):
    # The racer's sheet was not yet visible when we looked, so we create too; the re-list then shows both and
    # every racer picks the oldest. Our empty copy is renamed (never deleted) so nobody mistakes it for the real one.
    drive = _StubDrive({"racer": dict(id="racer", name="MH-500", mimeType=GSHEET, parents=["fold"],
                                      createdTime="2026-10-29T00:00:00+00:00")}, hide_until_created=True)
    g = _google(settings, monkeypatch, drive)
    assert g.create_sheet("fold", "MH-500", ["LAN ID"]) == "racer"
    ours = [f for f in drive.files_.values() if f["id"] != "racer"]
    assert len(ours) == 1 and "unused duplicate" in ours[0]["name"] and ours[0]["parents"] == ["fold"]


# ============================================================ M-5: move_rename serialised per file id

def test_concurrent_moves_of_one_file_never_leave_it_in_two_folders(settings, monkeypatch):
    drive = _StubDrive({"p": dict(id="p", name="x.pdf", mimeType=PDF, parents=["A"], createdTime="")}, slow_get=0.05)
    g = _google(settings, monkeypatch, drive)
    ts = [threading.Thread(target=g.move_rename, args=("p", dest)) for dest in ("B", "C")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(drive.files_["p"]["parents"]) == 1, drive.files_["p"]["parents"]


def test_move_rename_still_moves_and_renames_in_one_call(settings, monkeypatch):
    drive = _StubDrive({"p": dict(id="p", name="x.pdf", mimeType=PDF, parents=["A"], createdTime="")})
    g = _google(settings, monkeypatch, drive)
    g.move_rename("p", "B", "CERT1.pdf")
    assert drive.files_["p"]["parents"] == ["B"] and drive.files_["p"]["name"] == "CERT1.pdf"
    assert len(drive.updates) == 1


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
