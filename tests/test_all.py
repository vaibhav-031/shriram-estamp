"""Run: ESTAMP_TEST_DB_URL=postgresql://… python -m pytest -q tests/
The end-to-end tests create and drop their own schema (estamp_test); they never touch anything else.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from estamp.context import Ctx  # noqa: E402
from estamp.extract import (CertMatcher, StateResolver, analyze_paper, normalize_ocr,  # noqa: E402
                            parse_order_tabs)
from estamp.google_clients import FOLDER, GSHEET, PDF  # noqa: E402
from estamp.http_api import ApiAuthError  # noqa: E402
from estamp.settings import load_settings  # noqa: E402

import samples  # noqa: E402
from fake_google import FakeGoogle  # noqa: E402


@pytest.fixture(scope="session")
def settings():
    return pin_test_world(load_settings(ROOT / "config.yaml"))


def pin_test_world(s):
    """The fixtures predate the vendor sheet (2026-10-29): front + back tests use real traditional RJ scans, the
    end-to-end world stocks MH at ₹200 (₹100 is its "wrong amount"), and KL is the inactive state. Pin that world
    here; the shipped values are checked against the sheet in test_state_config_matches_vendor_sheet."""
    s.states["RJ"].pages_per_paper = 2
    s.states["MH"].amounts = [200]
    s.states["KL"].active = False
    return s


# ============================================================ pure logic

def test_projection_loaded(settings):
    assert len(settings.states) == 27
    assert sum(s.daily_qty for s in settings.states.values()) == 12_330
    assert settings.states["RJ"].amounts == [195]


def test_vendors_loaded(settings):
    assert set(settings.vendors) == {"PAPEREASE", "AGREXP", "SHABDA"}
    assert settings.vendors["PAPEREASE"].name == "Paper Ease" and settings.vendors["PAPEREASE"].active


@pytest.mark.parametrize("raw,code", [("KARTNATAKA", "KA"), ("ANDRA PRADESH", "AP"), ("UTTARPRADESH", "UP"),
                                      ("DELHI NCR", "DL"), ("KOLKATA", "WB"), ("JAMMU & KASHMIR", "JK"),
                                      ("gujarat", "GJ"), ("MH", "MH"), ("Telangana", "TS"), ("Sheet1", "")])
def test_state_aliases(settings, raw, code):
    assert StateResolver(settings.states).resolve(raw) == code


def test_cert_and_amount(settings):
    r = StateResolver(settings.states)
    m = CertMatcher(settings.get("intake.cert_patterns"), r)
    t = normalize_ocr("Certificate No. : I N-MH 9000000000000 1A\nStamp Duty Amount(Rs.) : 200 (Two Hundred)")
    a = analyze_paper(t, "", settings, m, lambda c: False)
    assert a.ok, a.issues
    assert (a.cert, a.state, a.amount) == ("IN-MH90000000000001A", "MH", 200)
    tg = analyze_paper("Certificate No. IN-TG12345678901234B Stamp Duty Amount(Rs.) 100", "", settings, m, lambda c: False)
    assert tg.state == "TS" and tg.ok
    bad = analyze_paper("Certificate No. IN-MH12345678901234B Stamp Duty Amount(Rs.) 100", "", settings, m, lambda c: False)
    assert "not an allowed amount" in bad.issues[0]
    kl = analyze_paper("IN-KL12345678901234B Stamp Duty Amount 200", "", settings, m, lambda c: False)
    assert any("inactive" in i for i in kl.issues)
    two = analyze_paper("IN-MH12345678901234B and IN-MH12345678901235C Stamp Duty Amount 200", "", settings, m, lambda c: False)
    assert any("more than one" in i for i in two.issues)
    dup = analyze_paper("IN-MH12345678901234B Stamp Duty Amount 200", "", settings, m, lambda c: True)
    assert any("duplicate" in i for i in dup.issues)


def test_order_sheets(settings):
    r = StateResolver(settings.states)
    res = {n: parse_order_tabs([(n, fn())], n, settings, r) for n, fn in samples.SHEETS.items()}
    mh = res["TEST_Shriram_MH_LANs_26Oct"]
    assert len(mh.valid) == 12 and len(mh.duplicates) == 1 and len(mh.invalid) == 1
    ka = res["TEST_Shriram_Karnataka_LANs_26Oct"]
    assert len(ka.valid) == 10 and {o.state for o in ka.valid} == {"KA"} and {o.denomination for o in ka.valid} == {500}
    tn = res["TEST_Shriram_TN_LANs_26Oct"]
    assert len(tn.valid) == 10 and {o.state for o in tn.valid} == {"TN"}
    gj = res["TEST_Shriram_GJ_LANs_26Oct"]
    assert len(gj.valid) == 11 and len(gj.invalid) == 1 and "250" in gj.invalid[0]
    up = res["TEST_Shriram_UP_LANs_26Oct"]
    assert len(up.valid) == 10 and up.valid[0].row_no == 5


# ============================================================ end to end

DB_URL = os.environ.get("ESTAMP_TEST_DB_URL")
e2e = pytest.mark.skipif(not DB_URL, reason="set ESTAMP_TEST_DB_URL to run end-to-end tests")


@pytest.fixture
def env(settings):
    import psycopg
    from estamp.db import DB
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS estamp_test CASCADE")
    settings.raw["db"]["schema"] = "estamp_test"
    settings.raw["dry_run"] = False
    settings.raw["orders"]["settle_minutes"] = 0
    settings.raw["intake"]["settle_minutes"] = 0
    # Default clients stay offline: pypdf split + an offline fake reader (_ctx / explicit ocr=). The live reader is
    # the in-house OCR API; tests opt into it with FakeApiOcr. (Drive OCR was removed in A-09.)
    settings.raw["intake"]["split_provider"] = "local"
    g = FakeGoogle()
    root = g.folder("Shriram eStamp (Python)")
    for key in ["upload_inbox", "upload_archive", "needs_review", "inventory_root", "orders_incoming",
                "orders_processed", "orders_rejected", "output_root", "backup_root", "reconciliation_root"]:
        settings.raw["folders"][key] = g.folder(key, root)
    settings.raw["control_sheet_id"] = g.add("control", GSHEET, root, tabs={})
    db = DB(DB_URL, "estamp_test", max_size=12)
    db.init_schema()
    yield settings, db, g
    db.close()


def _ctx(settings, db, g):
    from fake_apis import FakeOcr
    return Ctx(settings, db, g, ocr=FakeOcr())   # offline reader; stands in for the live OCR API in e2e


def _vendor_folder(g, s, name="Paper Ease"):
    """The inbox subfolder ops drop files into (D-1: files must sit inside a vendor folder, never the root)."""
    return g.folder_path(s.folder("upload_inbox"), [name])


def inbox_add(g, s, name, mime, data=b"", vendor="Paper Ease", **kw):
    """g.add into the inbox, but inside a vendor folder so intake will process it (root files are E101)."""
    return g.add(name, mime, _vendor_folder(g, s, vendor), data, **kw)


def _inbox_files(g, s):
    """Every non-folder file still in the inbox, at any depth (vendor folders stay; their contents are the uploads)."""
    files, _ = _intake_collect(g, s.folder("upload_inbox"))
    return files


def _intake_collect(g, folder_id):
    from estamp.intake import _collect
    return _collect(g, folder_id)


def _stock(g, s, state, amount, n, start=1):
    pages = [samples.paper_page(samples.cert(state, start + i), amount) for i in range(n)]
    return inbox_add(g, s, f"vendor_{state}_{amount}_{start}.pdf", PDF, samples.merged(pages))


@e2e
def test_full_flow(env):
    from estamp.cycle import run_cycle
    s, db, g = env
    # ---- orders (MH oldest so the GJ sheet's MH LAN creates a conflict)
    for age, (name, fn) in zip([50, 40, 30, 20, 10], samples.SHEETS.items()):
        g.add(name, GSHEET, s.folder("orders_incoming"), tabs={name: fn()}, age_min=age)
    # ---- vendor papers (merged PDFs, auto-routed by cert state + amount)
    _stock(g, s, "MH", 200, 14)
    ka = _stock(g, s, "KA", 500, 10)
    _stock(g, s, "TN", 200, 6)
    _stock(g, s, "GJ", 300, 10)
    _stock(g, s, "UP", 100, 10)
    g.add("vendor_KA_copy.pdf", PDF, _vendor_folder(g, s), g.files[ka]["data"])          # exact duplicate upload
    bad = samples.merged([samples.paper_page(samples.cert("MH", 900), 100),                  # wrong amount for MH
                          samples.paper_page(samples.cert("MH", 1), 200),                    # duplicate cert
                          samples.paper_page("", 200),                                       # no cert
                          samples.paper_page(samples.cert("KL", 1), 200)])                   # inactive state
    g.add("vendor_mixed_bad.pdf", PDF, _vendor_folder(g, s), bad)
    g.add("notes.txt", "text/plain", _vendor_folder(g, s), b"hello")

    ctx = _ctx(s, db, g)
    assert run_cycle(ctx)

    st = {r["status"]: r["n"] for r in db.q("SELECT status, count(*) n FROM orders GROUP BY 1")}
    assert st == {"DONE": 47, "PENDING": 4, "CONFLICT": 2}, st
    assert db.q1("SELECT count(*) n FROM papers WHERE status='NEEDS_REVIEW'")["n"] == 4
    reasons = " | ".join(r["notes"] for r in db.q("SELECT notes FROM papers WHERE status='NEEDS_REVIEW'"))
    for needle in ["not an allowed amount", "duplicate certificate", "no certificate / serial number", "inactive"]:
        assert needle in reasons, reasons
    assert db.q1("SELECT count(*) n FROM uploads WHERE status='DUPLICATE'")["n"] == 1

    # nothing allocated twice, every allocation's file really sits in Output as <LAN>.pdf
    allocs = db.q("SELECT * FROM allocations")
    assert len(allocs) == 47 and len({a["paper_id"] for a in allocs}) == 47
    for a in allocs:
        path = g.path_of(a["pdf_file_id"])
        assert path.endswith(f"{a['state']}/{a['state']}-{a['denomination']} PDFs/{a['lan']}.pdf"), path
        assert g.path_of(a["backup_file_id"]).startswith("backup_root/")
    # auto-nesting created Inventory/<STATE>/<AMOUNT>/ and spare MH papers wait there named by cert
    inv = [g.path_of(f["id"]) for f in g.files.values() if f["mimeType"] == PDF and "inventory_root/" in g.path_of(f["id"])]
    assert len(inv) == 3 and all(p.startswith("inventory_root/MH/200/IN-MH") for p in inv), inv
    # output sheets: one per state/amount for today
    outs = {g.files[sid]["name"]: rows for sid, tabs in g.sheets_data.items() if sid in g.files
            and g.files[sid]["name"].count("-") == 1 for rows in tabs.values()}
    assert len(outs["MH-200"]) == 1 + 11 and len(outs["TN-200"]) == 1 + 6
    # second OCR: every issued PDF was re-read and its printed cert matches the ledger
    for row in outs["MH-200"][1:]:
        assert row[1].startswith("IN-MH") and len(row) == 6, row
    assert db.q1("SELECT count(*) n FROM allocations WHERE verified_cert IS DISTINCT FROM cert_no")["n"] == 0
    # every order file left the inbox; the non-PDF and the duplicate upload went to Needs Review
    assert g.list_children(s.folder("orders_incoming")) == []
    assert _inbox_files(g, s) == []
    # dashboard
    ctl = g.sheets_data[s.get("control_sheet_id")]
    assert {"Summary", "Stock", "Needs Review", "Exceptions", "Log"} <= set(ctl)
    tn_row = [r for r in ctl["Stock"] if r[0] == "TN"][0]
    assert tn_row[4] == 0 and tn_row[6] == 4 and tn_row[13] == "SHORT"
    assert any("TN ₹200: 4 LAN(s) waiting" in a for a in ctx.alerts)

    # ---- cycle 2: ops actions + new stock
    rv = db.q1("SELECT id FROM papers WHERE notes LIKE %s", ("%no certificate / serial number%",))["id"]
    ctl["Review Actions"].append([str(rv), "APPROVE", samples.cert("MH", 777), "MH", "200"])
    ctl["Order Actions"].append(["TEST-MH-000005", "KEEP_STATE", "MH"])
    ctl["Order Actions"].append(["TEST-UP-000010", "CANCEL", ""])
    _stock(g, s, "TN", 200, 4, start=50)
    ctx2 = _ctx(s, db, g)
    assert run_cycle(ctx2)

    assert [r[6] for r in ctl["Review Actions"][1:]][0].startswith("DONE: approved")
    results = [r[3] for r in ctl["Order Actions"][1:]]
    assert results[0].startswith("DONE: MH kept") and results[1].startswith("DONE: cancelled, paper returned"), results
    st = {r["status"]: r["n"] for r in db.q("SELECT status, count(*) n FROM orders GROUP BY 1")}
    assert st == {"DONE": 51, "CANCELLED": 2}, st   # +4 TN, +MH-000005, −UP-000010; GJ copy of MH-000005 cancelled
    up_paper = db.q1("SELECT p.* FROM papers p JOIN orders o ON o.paper_id = p.id WHERE o.lan='TEST-UP-000010'")
    assert up_paper["status"] == "AVAILABLE" and g.path_of(up_paper["drive_file_id"]).startswith("inventory_root/UP/100/")
    assert outs["UP-100"][-1][5].startswith("CANCELLED")


@e2e
def test_active_run_age_reports_newest_unfinished_run(env):
    s, db, g = env
    assert db.active_run_age_seconds() is None                 # no runs yet
    done = db.start_run()
    db.finish_run(done, True, {})
    assert db.active_run_age_seconds() is None                 # finished runs don't count
    live = db.start_run()
    db.q("UPDATE runs SET started_at = now() - interval '40 minutes' WHERE id=%s", (live,))
    age = db.active_run_age_seconds()
    assert age is not None and age >= 40 * 60                  # age of the newest unfinished run


@e2e
def test_a_wedged_cycle_holder_raises_an_alert_instead_of_skipping_silently(env):
    import zlib

    import psycopg

    from estamp.cycle import run_cycle
    s, db, g = env
    # A second session holds the advisory lock and has a run that started 40 min ago but never finished –
    # i.e. a wedged process blocking every new cycle. The skip must escalate to an alert, not stay silent.
    holder = psycopg.connect(DB_URL, autocommit=True)
    holder.execute("SELECT pg_advisory_lock(%s)", (zlib.crc32(f"estamp:{db.schema}".encode()),))
    stuck = db.start_run()
    db.q("UPDATE runs SET started_at = now() - interval '40 minutes' WHERE id=%s", (stuck,))
    try:
        runs_before = db.q1("SELECT count(*) n FROM runs")["n"]
        ctx = _ctx(s, db, g)
        assert run_cycle(ctx)                                       # returns True (skipped)
        assert db.q1("SELECT count(*) n FROM runs")["n"] == runs_before, "a skipped cycle must not start a new run"
        wedged = [e["details"] for e in db.q("SELECT details FROM events WHERE level='ERROR' AND action='Cycle'")]
        assert any("wedged" in d for d in wedged), wedged
        assert any("wedged" in a for a in ctx.alerts)
    finally:
        holder.close()


@e2e
def test_a_brief_overlap_skips_quietly_without_an_alert(env):
    import zlib

    import psycopg

    from estamp.cycle import run_cycle
    s, db, g = env
    # Holder started just now (normal overlap of two cycles) – no alert, just a quiet skip.
    holder = psycopg.connect(DB_URL, autocommit=True)
    holder.execute("SELECT pg_advisory_lock(%s)", (zlib.crc32(f"estamp:{db.schema}".encode()),))
    db.start_run()
    try:
        ctx = _ctx(s, db, g)
        assert run_cycle(ctx)
        assert db.q1("SELECT count(*) n FROM events WHERE level='ERROR'")["n"] == 0, "brief overlap must not alert"
        assert ctx.alerts == []
    finally:
        holder.close()


@e2e
def test_active_run_age_reports_newest_unfinished_run(env):
    s, db, g = env
    assert db.active_run_age_seconds() is None                 # no runs yet
    done = db.start_run()
    db.finish_run(done, True, {})
    assert db.active_run_age_seconds() is None                 # finished runs don't count
    live = db.start_run()
    db.q("UPDATE runs SET started_at = now() - interval '40 minutes' WHERE id=%s", (live,))
    age = db.active_run_age_seconds()
    assert age is not None and age >= 40 * 60                  # age of the newest unfinished run


@e2e
def test_a_wedged_cycle_holder_raises_an_alert_instead_of_skipping_silently(env):
    import zlib

    import psycopg

    from estamp.cycle import run_cycle
    s, db, g = env
    # A second session holds the advisory lock and has a run that started 40 min ago but never finished –
    # i.e. a wedged process blocking every new cycle. The skip must escalate to an alert, not stay silent.
    holder = psycopg.connect(DB_URL, autocommit=True)
    holder.execute("SELECT pg_advisory_lock(%s)", (zlib.crc32(f"estamp:{db.schema}".encode()),))
    stuck = db.start_run()
    db.q("UPDATE runs SET started_at = now() - interval '40 minutes' WHERE id=%s", (stuck,))
    try:
        runs_before = db.q1("SELECT count(*) n FROM runs")["n"]
        ctx = _ctx(s, db, g)
        assert run_cycle(ctx)                                       # returns True (skipped)
        assert db.q1("SELECT count(*) n FROM runs")["n"] == runs_before, "a skipped cycle must not start a new run"
        wedged = [e["details"] for e in db.q("SELECT details FROM events WHERE level='ERROR' AND action='Cycle'")]
        assert any("wedged" in d for d in wedged), wedged
        assert any("wedged" in a for a in ctx.alerts)
    finally:
        holder.close()


@e2e
def test_a_brief_overlap_skips_quietly_without_an_alert(env):
    import zlib

    import psycopg

    from estamp.cycle import run_cycle
    s, db, g = env
    # Holder started just now (normal overlap of two cycles) – no alert, just a quiet skip.
    holder = psycopg.connect(DB_URL, autocommit=True)
    holder.execute("SELECT pg_advisory_lock(%s)", (zlib.crc32(f"estamp:{db.schema}".encode()),))
    db.start_run()
    try:
        ctx = _ctx(s, db, g)
        assert run_cycle(ctx)
        assert db.q1("SELECT count(*) n FROM events WHERE level='ERROR'")["n"] == 0, "brief overlap must not alert"
        assert ctx.alerts == []
    finally:
        holder.close()


@e2e
def test_crash_recovery_and_dry_run(env):
    from estamp.cycle import run_cycle
    s, db, g = env
    g.add("TEST_Shriram_MH_LANs_26Oct", GSHEET, s.folder("orders_incoming"), tabs={"x": samples.mh()})
    _stock(g, s, "MH", 200, 2)
    s.raw["dry_run"] = True
    ctx = _ctx(s, db, g)
    assert run_cycle(ctx)
    assert db.q1("SELECT count(*) n FROM orders")["n"] == 0 and db.q1("SELECT count(*) n FROM papers")["n"] == 0
    assert len(_inbox_files(g, s)) == 1, "dry run must not move anything"
    kinds = {r[0] for r in ctx.preview}
    assert {"PAPER", "ORDER", "ALLOCATE"} <= kinds
    alloc = [r for r in ctx.preview if r[0] == "ALLOCATE"]
    assert len(alloc) == 12 and sum(r[7].startswith("OK") for r in alloc) == 2, alloc   # 2 papers for 12 LANs
    assert "Dry Run Preview" in g.sheets_data[s.get("control_sheet_id")]

    s.raw["dry_run"] = False
    assert run_cycle(_ctx(s, db, g))
    # simulate a crash mid-allocation: an order left ALLOCATING 30 min ago
    db.q("UPDATE orders SET status='ALLOCATING', updated_at=now()-interval '30 minutes' WHERE id=(SELECT min(id) FROM orders WHERE status='PENDING')")
    ctx3 = _ctx(s, db, g)
    run_cycle(ctx3)
    assert db.q1("SELECT count(*) n FROM orders WHERE status='ERROR'")["n"] == 1
    assert any("stuck mid-allocation" in a for a in ctx3.alerts)


@e2e
def test_issue_time_ocr_catches_swapped_file(env):
    """A paper's file is swapped after intake (wrong PDF in the folder). The second OCR must stop it."""
    from estamp.cycle import run_cycle
    s, db, g = env
    _stock(g, s, "MH", 200, 2)
    assert run_cycle(_ctx(s, db, g))                       # intake only, no orders yet
    papers = db.q("SELECT * FROM papers WHERE status='AVAILABLE' ORDER BY added_at, id")
    assert len(papers) == 2
    first = papers[0]
    g.files[first["drive_file_id"]]["data"] = samples.paper_page(samples.cert("MH", 555), 200)   # tamper
    g.add("TEST_Shriram_MH_LANs_26Oct", GSHEET, s.folder("orders_incoming"),
          tabs={"x": [samples.H, ["1", "LAN-A", "c", "b", "MH", "200"], ["2", "LAN-B", "c", "b", "MH", "200"]]})
    ctx = _ctx(s, db, g)
    run_cycle(ctx)
    st = {r["lan"]: r["status"] for r in db.q("SELECT lan, status FROM orders")}
    assert sorted(st.values()) == ["DONE", "ERROR"], st
    bad = db.q1("SELECT * FROM papers WHERE id=%s", (first["id"],))
    assert bad["status"] == "ERROR" and "ledger expected" in bad["notes"]
    assert "Cert mismatch" in g.path_of(bad["drive_file_id"])
    out = [rows for sid, tabs in g.sheets_data.items() if sid in g.files and g.files[sid]["name"] == "MH-200"
           for rows in tabs.values()][0]
    assert len(out) == 2 and out[1][1].startswith("IN-MH")    # only the good one was issued
    assert any("ledger expected" in a for a in ctx.alerts)


@e2e
def test_output_never_appends_a_lan_twice_across_a_crash(env):
    """E-01: crash after append_rows but before mark_written commits must not duplicate a LAN.
    Allocate one LAN, run outputs with mark_written crashing (row lands in the sheet, ledger NOT flagged),
    then run outputs again normally – the sheet must still hold exactly one row for that LAN."""
    from estamp.cycle import run_cycle
    from estamp.outputs import run_outputs

    s, db, g = env
    _stock(g, s, "MH", 200, 1)
    g.add("TEST_Shriram_MH_LANs_26Oct", GSHEET, s.folder("orders_incoming"),
          tabs={"x": [samples.H, ["1", "LAN-ONLY", "c", "b", "MH", "200"]]})

    # first cycle: intake + issue, but make the output write "crash" after append_rows
    ctx = _ctx(s, db, g)
    orig = db.mark_written
    db.mark_written = lambda ids: (_ for _ in ()).throw(RuntimeError("crash after append"))
    run_cycle(ctx)
    db.mark_written = orig

    def mh200():
        return [rows for sid, tabs in g.sheets_data.items() if sid in g.files
                and g.files[sid]["name"] == "MH-200" for rows in tabs.values()][0]

    assert len(mh200()) == 2, "one header + one issued LAN after the crash"
    assert db.q1("SELECT count(*) n FROM allocations WHERE NOT output_written")["n"] == 1, "crash left it unwritten"

    # next cycle re-fetches the same allocation as unwritten – must NOT append it again
    run_outputs(_ctx(s, db, g))
    rows = mh200()
    assert len(rows) == 2, f"LAN appended twice after crash+rerun: {rows}"
    assert [r[0] for r in rows[1:]] == ["LAN-ONLY"]
    assert db.q1("SELECT count(*) n FROM allocations WHERE NOT output_written")["n"] == 0, "rerun flagged it"

    # A later CANCELLED row for the same LAN (written by the cancel action, not run_outputs) is still allowed,
    # and a further run_outputs must leave it in place (the guard keys on LAN presence, not status).
    label = next(t for t in g.sheets_data[next(sid for sid, f in g.files.items() if f["name"] == "MH-200")])
    sid = next(sid for sid, f in g.files.items() if f["name"] == "MH-200")
    g.append_rows(sid, label, [["LAN-ONLY", "IN-MH90000000000001A", "", "", "2026-10-28 02:00:00", "CANCELLED – do not use"]])
    run_outputs(_ctx(s, db, g))
    rows = mh200()
    assert len(rows) == 3 and rows[2][5].startswith("CANCELLED"), rows


# ============================================================ D-2/D-3 report + vendor reconciliation

def _alloc(db, lan, state, denom, d, cancelled=False):
    """Seed one allocation (and a backing paper) straight into the ledger for report tests."""
    pid = db.insert_paper(state=state, denomination=denom, status="ASSIGNED", cert_no=f"C-{lan}")
    oid = db.q1("""INSERT INTO orders(lan, lan_key, state, denomination, status)
                   VALUES (%s,%s,%s,%s,'DONE') RETURNING id""", (lan, lan.lower(), state, denom))["id"]
    db.q("""INSERT INTO allocations(order_id, paper_id, lan, cert_no, state, denomination, alloc_date, cancelled_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s, CASE WHEN %s THEN now() ELSE NULL END)""",
         (oid, pid, lan, f"C-{lan}", state, denom, d, cancelled))


@e2e
def test_report_per_state_rows_and_total(env):
    """One row per (state, stamp duty); a state with two amounts yields two rows; a Total row at the end.
    Quantity counts non-cancelled allocations issued that date; cancelled ones never count. Amount and the
    totals are sheet formulas (so ops edits recalc)."""
    from datetime import date

    from estamp.report import run_report
    s, db, g = env
    d = date(2026, 10, 28)
    _alloc(db, "L1", "MH", 200, d)
    _alloc(db, "L2", "MH", 200, d)
    _alloc(db, "L3", "MH", 500, d)                 # MH has two amounts → two rows
    _alloc(db, "L4", "KA", 500, d)
    _alloc(db, "L5", "KA", 500, d, cancelled=True)  # cancelled → not counted
    _alloc(db, "L6", "TN", 200, date(2026, 10, 27))  # different date → excluded

    sid = run_report(_ctx(s, db, g), d, d)
    rows = g.sheets_data[sid]["Daily Report"]
    assert rows[0] == ["State", "Stamp Duty (₹)", "Quantity", "Amount (₹)"]
    body = rows[1:-1]
    quantities = {(r[0], r[1]): r[2] for r in body}
    assert quantities == {("MH", 200): 2, ("MH", 500): 1, ("KA", 500): 1}, body
    assert len([r for r in body if r[0] == "MH"]) == 2, "state with two amounts → two rows"
    assert "TN" not in {r[0] for r in body}, "other dates excluded"
    for r in body:
        assert str(r[3]).startswith("="), f"Amount must be a formula: {r}"
    total = rows[-1]
    assert total[0] == "Total"
    assert str(total[2]).startswith("=") and str(total[3]).startswith("="), "totals must be formulas"
    assert g.path_of(sid).startswith("reconciliation_root/2026-10-28/"), g.path_of(sid)


@e2e
def test_report_rerun_replaces_the_sheet(env):
    """Re-running the same date replaces the sheet – one Daily Report sheet for that date (reused, not deleted)."""
    from datetime import date

    from estamp.report import run_report
    s, db, g = env
    d = date(2026, 10, 28)
    _alloc(db, "L1", "MH", 200, d)
    sid1 = run_report(_ctx(s, db, g), d, d)
    _alloc(db, "L2", "MH", 200, d)
    sid2 = run_report(_ctx(s, db, g), d, d)

    assert sid1 == sid2, "re-run must reuse the same sheet, never create a second"
    daily = [f for f in g.files.values() if f["name"] == "Daily Report" and not f["trashed"]]
    assert len(daily) == 1, f"re-run must replace, not accumulate: {len(daily)} sheets"
    assert g.sheets_data[sid2]["Daily Report"][1][2] == 2, "re-run reflects the newer counts"
    assert any("replace" in e["details"].lower() for e in db.q("SELECT details FROM events")), "replacement logged"


@e2e
def test_vendor_reconciliation_balances_and_flags_e402(env):
    """Per vendor × state × stamp duty: received / issued / in stock / in review / rejected-or-duplicate.
    Balance check passes when received = issued + in stock + in review + rejected; a mismatch emits E402."""
    from datetime import date

    from estamp.report import run_report
    s, db, g = env
    # PAPEREASE / MH / 200: 1 issued + 1 available + 1 review + 1 rejected = 4 received → balances
    db.insert_paper(vendor_code="PAPEREASE", state="MH", denomination=200, status="ASSIGNED", cert_no="A1")
    db.insert_paper(vendor_code="PAPEREASE", state="MH", denomination=200, status="AVAILABLE", cert_no="A2")
    db.insert_paper(vendor_code="PAPEREASE", state="MH", denomination=200, status="NEEDS_REVIEW", cert_no="A3")
    db.insert_paper(vendor_code="PAPEREASE", state="MH", denomination=200, status="REJECTED", cert_no="A4")

    d = date(2026, 10, 28)
    sid = run_report(_ctx(s, db, g), d, d)
    recon = g.sheets_data[sid]["Vendor Reconciliation"]
    hdr = recon[0]
    for col in ["Rate per paper (₹)", "Payable", "Disputed", "Net payable"]:
        assert any(col in c for c in hdr), hdr
    row = next(r for r in recon[1:] if r[0] == "PAPEREASE")
    # received, issued, in stock, in review, rejected columns (after vendor/state/duty)
    assert row[3:8] == [4, 1, 1, 1, 1], row
    assert all(str(c).startswith("=") for c in row[-3:]), row  # Payable / Disputed / Net payable are formulas
    assert not any("E402" in e["details"] for e in db.q("SELECT details FROM events")), "balanced → no E402"

    # Now make it NOT balance: a RESERVED paper is received but in none of the five buckets.
    db.insert_paper(vendor_code="PAPEREASE", state="MH", denomination=200, status="RESERVED", cert_no="A5")
    run_report(_ctx(s, db, g), d, d)
    assert any("E402" in e["details"] for e in db.q("SELECT details FROM events")), "mismatch → E402"


@e2e
def test_inbox_routes_by_vendor_folder(env):
    """D-1: a file's vendor is the top-level inbox subfolder it descends from. Nested subfolders inherit it; a file
    directly in the inbox root is E101 and is NOT processed or moved; a top-level folder that is not a known vendor
    is also E101. The vendor_code is stamped on every paper stocked from that upload."""
    from estamp.cycle import run_cycle
    s, db, g = env
    inbox = s.folder("upload_inbox")
    pe = g.folder("Paper Ease", inbox)                 # recognised by vendor_name
    nested = g.folder("batch 2", pe)                   # nested → inherits Paper Ease
    g.add("p1.pdf", PDF, pe, samples.paper_page(samples.cert("RJ", 1), 195))
    g.add("p2.pdf", PDF, nested, samples.merged([samples.paper_page(samples.cert("RJ", 2), 195),
                                                 samples.paper_page(samples.cert("RJ", 3), 195)]))
    agr = g.folder("AGREXP", inbox)                    # recognised by vendor_code
    g.add("p3.pdf", PDF, agr, samples.paper_page(samples.cert("MH", 1), 200))
    unknown = g.folder("Random Vendor", inbox)         # not a known vendor → E101
    uid = g.add("rogue.pdf", PDF, unknown, samples.paper_page(samples.cert("MH", 2), 200))
    loose = g.add("loose.pdf", PDF, inbox, samples.paper_page(samples.cert("MH", 3), 200))  # root → E101

    assert run_cycle(_ctx(s, db, g))

    stocked = {r["cert_no"]: r["vendor_code"] for r in db.q("SELECT cert_no, vendor_code FROM papers WHERE status='AVAILABLE'")}
    assert stocked == {samples.cert("RJ", 1): "PAPEREASE", samples.cert("RJ", 2): "PAPEREASE",
                       samples.cert("RJ", 3): "PAPEREASE", samples.cert("MH", 1): "AGREXP"}, stocked
    # E101 files are left exactly where they are – never stocked, never moved.
    assert g.files[loose]["parents"] == [inbox], "root file must not move"
    assert g.files[uid]["parents"] == [unknown], "unknown-vendor file must not move"
    assert db.q1("SELECT count(*) n FROM papers WHERE cert_no IN (%s,%s)",
                 (samples.cert("MH", 2), samples.cert("MH", 3)))["n"] == 0
    assert "E101" in " ".join(e["details"] for e in db.q("SELECT details FROM events WHERE level='WARN'"))


@e2e
def test_volume_no_double_issue(env):
    """~1,000 LANs over four state + stamp duty groups (TN short by 50): every LAN gets at most one paper,
    no paper is issued twice, and the LANs without stock stay PENDING. Guard for every phase (definition of done)."""
    from estamp.cycle import run_cycle
    from fake_apis import FakeOcr
    s, db, g = env
    groups = [("MH", 200, 300, 300), ("UP", 100, 300, 300), ("GJ", 300, 200, 200), ("TN", 200, 200, 150)]
    rows = [samples.H]
    for st, amt, lans, papers in groups:
        rows += [[str(len(rows)), f"VOL-{st}-{i:05d}", "c", "b", st, str(amt)] for i in range(lans)]
        _stock(g, s, st, amt, papers)
    g.add("volume_orders", GSHEET, s.folder("orders_incoming"), tabs={"x": rows})

    for _ in range(3):                       # a cycle is time-boxed; repeat until nothing is left to issue
        assert run_cycle(Ctx(s, db, g, ocr=FakeOcr()))   # fake reader: independent of Drive OCR / text layer
        if not db.pending_groups() or all(grp["avail"] == 0 for grp in db.pending_groups()):
            break

    st = {r["status"]: r["n"] for r in db.q("SELECT status, count(*) n FROM orders GROUP BY 1")}
    assert st == {"DONE": 950, "PENDING": 50}, st
    assert {r["state"] for r in db.q("SELECT state FROM orders WHERE status='PENDING'")} == {"TN"}
    allocs = db.q("SELECT * FROM allocations WHERE cancelled_at IS NULL")
    assert len(allocs) == 950
    assert len({a["paper_id"] for a in allocs}) == 950, "a paper was issued twice"
    assert len({a["order_id"] for a in allocs}) == 950, "a LAN got two papers"
    assert len({a["pdf_file_id"] for a in allocs}) == 950
    assert db.q1("SELECT count(*) n FROM papers WHERE status='ASSIGNED'")["n"] == 950
    assert db.q1("SELECT count(*) n FROM papers WHERE status NOT IN ('ASSIGNED')")["n"] == 0
    for a in allocs:
        assert g.path_of(a["pdf_file_id"]).endswith(f"/{a['lan']}.pdf")


@e2e
def test_intake_and_issue_check_read_through_the_context_clients(env):
    """Intake splits and reads, and the issue check reads, only through the run context's split + OCR clients."""
    from estamp.cycle import run_cycle
    from fake_apis import FakeOcr, FakeSplitter
    s, db, g = env
    _stock(g, s, "MH", 200, 2)
    g.add("orders", GSHEET, s.folder("orders_incoming"),
          tabs={"x": [samples.H, ["1", "LAN-A", "c", "b", "MH", "200"]]})
    ocr, splitter = FakeOcr(), FakeSplitter()
    assert run_cycle(Ctx(s, db, g, ocr=ocr, splitter=splitter))
    assert splitter.calls == [("vendor_MH_200_1.pdf", 1)]
    assert sorted(ocr.calls) == ["LAN-A.pdf_p1", "vendor_MH_200_1.pdf_p1", "vendor_MH_200_1.pdf_p2"]
    a = db.q1("SELECT * FROM allocations")
    assert a["lan"] == "LAN-A" and a["verified_via"] == "fake"


@e2e
def test_ocr_failure_keeps_the_whole_upload_in_the_inbox(env):
    """A-08: a read failure leaves the whole upload in the Upload Inbox for the next cycle – nothing half-stocked."""
    from estamp.cycle import run_cycle
    from fake_apis import FakeOcr
    s, db, g = env
    up = _stock(g, s, "MH", 200, 3)
    assert run_cycle(Ctx(s, db, g, ocr=FakeOcr(fail={"vendor_MH_200_1.pdf_p2": RuntimeError("OCR API HTTP 503")})))
    assert db.q1("SELECT count(*) n FROM papers")["n"] == 0
    assert [f["id"] for f in _inbox_files(g, s)] == [up]
    assert db.q1("SELECT status FROM uploads")["status"] == "PROCESSING"   # still retryable next cycle


@e2e
def test_ocr_failure_escalates_to_needs_review_after_the_give_up_window(env):
    """A-08/D1: once an upload has failed to read for longer than intake.ocr_fail_give_up_minutes, the WHOLE upload
    moves to Needs Review (not stocked page by page) with a clear reason + ops alert, instead of retrying forever."""
    from estamp.cycle import run_cycle
    from fake_apis import FakeOcr
    s, db, g = env
    up = _stock(g, s, "MH", 200, 3)
    ocr = FakeOcr(fail={"vendor_MH_200_1.pdf_p2": RuntimeError("OCR API HTTP 503")})
    # First cycle: failure, upload stays in the inbox, first-failure time recorded as now().
    assert run_cycle(Ctx(s, db, g, ocr=ocr))
    assert [f["id"] for f in _inbox_files(g, s)] == [up]
    # Simulate the give-up window having elapsed since the first failure.
    give_up = int(s.get("intake.ocr_fail_give_up_minutes", 30))
    db.q("UPDATE uploads SET created_at = now() - make_interval(mins => %s)", (give_up + 1,))
    assert run_cycle(Ctx(s, db, g, ocr=ocr))
    assert db.q1("SELECT count(*) n FROM papers")["n"] == 0, "whole upload to review, never stocked page by page"
    assert db.q1("SELECT status FROM uploads")["status"] == "REVIEW"
    assert "OCR" in (db.q1("SELECT notes FROM uploads")["notes"] or "")
    assert _inbox_files(g, s) == [], "upload left the inbox"
    assert db.q1("SELECT count(*) n FROM events WHERE action='Needs review' AND level='WARN'")["n"] >= 1


@e2e
def test_ocr_auth_failure_stops_the_upload_loudly_without_retrying(env):
    """A-08: a 401/403 (ApiAuthError) must surface clearly and keep the upload in the inbox – not loop silently."""
    from estamp.cycle import run_cycle
    from fake_apis import FakeOcr
    s, db, g = env
    up = _stock(g, s, "MH", 200, 2)
    ocr = FakeOcr(fail={"vendor_MH_200_1.pdf_p1": ApiAuthError("refused (HTTP 401)")})
    assert run_cycle(Ctx(s, db, g, ocr=ocr))
    assert db.q1("SELECT count(*) n FROM papers")["n"] == 0
    assert [f["id"] for f in _inbox_files(g, s)] == [up]
    assert db.q1("SELECT count(*) n FROM events WHERE level='ERROR' AND details ILIKE '%401%'")["n"] >= 1


@e2e
def test_non_pdf_upload_event_carries_the_error_code(env):
    """A non-PDF upload is routed to Needs Review/Not PDF and its ops event carries the E102 code."""
    from estamp.cycle import run_cycle
    s, db, g = env
    g.add("notes.txt", "text/plain", _vendor_folder(g, s), b"hello")
    assert run_cycle(_ctx(s, db, g))
    assert db.q1("SELECT count(*) n FROM events WHERE level='WARN' AND details LIKE '%E102%'")["n"] >= 1


# ============================================================ split API (replays docs/api-samples/split)

SPLIT_SAMPLES = ROOT / "docs" / "api-samples" / "split"


class _Resp:
    def __init__(self, status, content, ctype="application/octet-stream"):
        self.status_code, self.content, self.headers = status, content, {"content-type": ctype}

    def json(self):
        import json
        return json.loads(self.content)


@pytest.fixture
def split_api(settings, monkeypatch):
    """split_provider=zoop_api with requests.post replaced by a recorder that returns a scripted response."""
    import estamp.http_api as http_api
    import estamp.ocr_split as ocr_split
    monkeypatch.setitem(settings.raw["intake"], "split_provider", "zoop_api")
    monkeypatch.setattr(http_api, "_sleep", lambda s: None)          # retries/backoff without real waiting
    monkeypatch.setattr(http_api, "_limiters", {})
    calls, reply = [], {}

    def post(url, **kw):
        calls.append({"url": url, **kw})
        if "raise" in reply:
            raise reply["raise"]
        if "seq" in reply:
            return reply["seq"].pop(0)
        return reply["resp"]

    monkeypatch.setattr(ocr_split.requests, "post", post)
    return calls, reply


def _pages(data: bytes) -> int:
    import io
    from pypdf import PdfReader
    return len(PdfReader(io.BytesIO(data)).pages)


def test_split_api_digital_one_page_per_part(settings, split_api):
    from estamp.ocr_split import split_pdf
    calls, reply = split_api
    reply["resp"] = _Resp(200, (SPLIT_SAMPLES / "response_splitSize1.zip").read_bytes())
    pdf = (SPLIT_SAMPLES / "test_input_4pages.pdf").read_bytes()
    parts = split_pdf(pdf, "vendor.pdf", settings)
    assert [(p.page_from, p.page_to) for p in parts] == [(1, 1), (2, 2), (3, 3), (4, 4)]
    assert all(_pages(p.data) == 1 for p in parts)
    (c,) = calls
    assert c["url"] == "https://live.zoopsign.com/api/pdf-utils/split"
    assert c["data"] == {"ranges[0][startPage]": "1", "ranges[0][endPage]": "4", "splitSize": "1"}
    assert c["files"]["file"][0] == "vendor.pdf" and c["files"]["file"][1] == pdf
    assert "headers" not in c or not c["headers"]          # no auth header
    assert c["timeout"]


def test_split_api_traditional_two_pages_per_part(settings, split_api):
    from estamp.ocr_split import split_pdf
    calls, reply = split_api
    reply["resp"] = _Resp(200, (SPLIT_SAMPLES / "response_splitSize2.zip").read_bytes())
    parts = split_pdf((SPLIT_SAMPLES / "test_input_4pages.pdf").read_bytes(), "scan.pdf", settings, split_size=2)
    assert [(p.page_from, p.page_to) for p in parts] == [(1, 2), (3, 4)]
    assert all(_pages(p.data) == 2 for p in parts)
    assert calls[0]["data"]["splitSize"] == "2"


def _zip_without(sample: str, drop: str) -> bytes:
    import io, zipfile
    src, buf = zipfile.ZipFile(SPLIT_SAMPLES / sample), io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for n in src.namelist():
            if n != drop:
                out.writestr(n, src.read(n))
    return buf.getvalue()


@pytest.mark.parametrize("make_reply,needle", [
    (lambda: {"resp": _Resp(500, (SPLIT_SAMPLES / "response_not_pdf.json").read_bytes(), "application/json")},
     "HTTP 500: Failed to parse PDF document"),
    (lambda: {"resp": _Resp(200, b"<html>gateway error</html>", "text/html")}, "not a zip"),
    (lambda: {"resp": _Resp(200, _zip_without("response_splitSize1.zip", "-3-3.pdf"))}, "don't cover pages 1-4"),
    (lambda: {"raise": requests.Timeout()}, "timed out"),
    (lambda: {"raise": requests.ConnectionError("Max retries exceeded with url ... pool=<urllib3 …>")},
     "unreachable"),
    (lambda: {"resp": _Resp(502, b'{"error":"upstream"}', "application/json")}, 'HTTP 502: {"error":"upstream"}'),
])
def test_split_api_failures_raise_clear_errors(settings, split_api, make_reply, needle):
    from estamp.ocr_split import split_pdf
    _, reply = split_api
    reply.update(make_reply())
    with pytest.raises(RuntimeError, match=needle):
        split_pdf((SPLIT_SAMPLES / "test_input_4pages.pdf").read_bytes(), "vendor.pdf", settings)


def test_split_api_is_rate_limited_and_retried(settings, split_api):
    """A-07: a 503 blip is retried through the shared split limiter and then succeeds."""
    import estamp.http_api as http_api
    from estamp.ocr_split import split_pdf
    calls, reply = split_api
    reply["seq"] = [_Resp(503, b"{}", "application/json"),
                    _Resp(200, (SPLIT_SAMPLES / "response_splitSize1.zip").read_bytes())]
    parts = split_pdf((SPLIT_SAMPLES / "test_input_4pages.pdf").read_bytes(), "vendor.pdf", settings)
    assert len(parts) == 4 and len(calls) == 2
    assert [k[0] for k in http_api._limiters] == ["split"]                      # its own limiter


def _ocr_resp(fields: dict, code: int = 200, message: str = "Success") -> "_Resp":
    """A 200 from the OCR API, shaped like the captured contract (docs/api-samples/ocr/README.md)."""
    import json
    pr = {"api_version": "v1", "success": True, "mode": "sync", "config_id": "ocrc_test", "response_code": code,
          "message": message, "fields": fields, "metadata": {}, "time_taken": {"total_processing_time_ms": 900}}
    body = {"success": True, "message": "ocr processed successfully", "status_code": 200,
            "data": {"ocr_batch_id": "ocbt_test", "ocr_transaction_id": "ocrt_test", "provider_response": pr,
                     "provider_response_raw": pr}}
    return _Resp(200, json.dumps(body).encode(), "application/json")


@pytest.fixture
def ocr_api(settings, split_api, monkeypatch):
    """OCR API configured with fake credentials; requests.post is the split_api recorder."""
    monkeypatch.setenv("ZOOP_OCR_API_KEY", "test-key-not-real")
    monkeypatch.setenv("ZOOP_OCR_ORG_ACC", "test-org-not-real")
    return split_api


def test_ocr_api_request_and_fields(settings, ocr_api):
    import base64
    from estamp.ocr_split import _ocr_zoop
    calls, reply = ocr_api
    fields = _f(certification_no="IN-HP90000000000001A", state_code="HP", stamp_duty_amount="100")
    reply["resp"] = _ocr_resp(fields)
    r = _ocr_zoop(b"%PDF-page", "up.pdf_p1", settings)
    assert r.fields == fields and r.via == "zoop_api" and r.raw["data"]["provider_response"]["response_code"] == 200
    (c,) = calls
    assert c["url"] == settings.get("zoop_api.ocr.url")
    assert c["headers"]["x-zoop-key"] == "test-key-not-real" and c["headers"]["x-zoop-org-acc"] == "test-org-not-real"
    assert c["json"] == {"mode": "sync", "ocr_config_id": settings.get("zoop_api.ocr.config_id"),
                         "documents": [{"data": base64.b64encode(b"%PDF-page").decode()}]}
    assert c["timeout"] == settings.get("zoop_api.ocr.timeout_s")


def test_ocr_endpoint_is_taken_from_env_over_config(settings, ocr_api, monkeypatch):
    """Production OCR endpoint comes from .env (ESTAMP_OCR_URL) and overrides the dev url in config.yaml."""
    from estamp.ocr_split import _ocr_zoop
    calls, reply = ocr_api
    monkeypatch.setenv("ESTAMP_OCR_URL", "https://prod-ocr.example/do")
    reply["resp"] = _ocr_resp(_f(certification_no="IN-HP90000000000001A", state_code="HP", stamp_duty_amount="100"))
    _ocr_zoop(b"%PDF-page", "up.pdf_p1", settings)
    assert calls[-1]["url"] == "https://prod-ocr.example/do"
    assert settings.get("zoop_api.ocr.url") != "https://prod-ocr.example/do"   # env won over the config fallback


def test_ocr_api_not_found_is_a_reading_not_an_error(settings, ocr_api):
    """211 'Certificate number not found' (e.g. a back page) comes back as empty fields – the checks decide."""
    from estamp.ocr_split import _ocr_zoop
    _, reply = ocr_api
    reply["resp"] = _ocr_resp(_f(state_code="MP"), 211, "Certificate number not found")
    assert _ocr_zoop(b"%PDF-page", "p2", settings).fields == _f(state_code="MP")


@pytest.mark.parametrize("make_reply,err,needle", [
    (lambda: {"resp": _Resp(422, b'{"success":false,"error":{"code":422,"type":"validation","message":"documents[0] '
                                 b'only application/pdf ... accepted"}}', "application/json")},
     RuntimeError, "OCR API HTTP 422: documents\\[0\\] only application/pdf"),
    (lambda: {"resp": _Resp(401, b'{"success":false,"error":{"message":"invalid key"}}', "application/json")},
     ApiAuthError, "ocr API refused the request \\(HTTP 401\\)"),
    (lambda: {"resp": _Resp(200, b"<html>proxy</html>", "text/html")}, RuntimeError, "not JSON"),
    (lambda: {"resp": _Resp(200, b'{"success":true,"data":{}}', "application/json")}, RuntimeError, "unexpected"),
    (lambda: {"raise": requests.Timeout()}, RuntimeError, "timed out"),
    (lambda: {"raise": requests.ConnectionError("pool…")}, RuntimeError, "unreachable"),
])
def test_ocr_api_failures_raise_clear_errors(settings, ocr_api, make_reply, err, needle):
    from estamp.ocr_split import _ocr_zoop
    _, reply = ocr_api
    reply.update(make_reply())
    with pytest.raises(err, match=needle):
        _ocr_zoop(b"%PDF-page", "p1", settings)


def test_ocr_api_without_credentials_never_calls(settings, ocr_api, monkeypatch):
    from estamp.ocr_split import _ocr_zoop
    calls, _ = ocr_api
    monkeypatch.delenv("ZOOP_OCR_ORG_ACC")
    with pytest.raises(RuntimeError, match="ZOOP_OCR_ORG_ACC"):
        _ocr_zoop(b"%PDF-page", "p1", settings)
    assert calls == []


def test_ocr_api_calls_go_through_the_shared_ocr_limiter(settings, ocr_api):
    """A-07: OCR calls (intake and issue check use the same adapter) are rate-limited and retried, on their own
    limiter – separate from the split API's."""
    import estamp.http_api as http_api
    from estamp.ocr_split import _ocr_zoop
    calls, reply = ocr_api
    reply["seq"] = [_Resp(503, b"{}", "application/json"),
                    _ocr_resp(_f(certification_no="IN-MH90000000000001A", state_code="MH"))]
    r = _ocr_zoop(b"%PDF-fake", "p1", settings)
    assert _field_value(r.fields, "certification_no") == "IN-MH90000000000001A" and len(calls) == 2
    assert [k[0] for k in http_api._limiters] == ["ocr"]


def _field_value(fields, name):
    return (fields.get(name) or {}).get("value")


def test_split_api_rejects_a_pdf_with_no_pages_without_calling(settings, split_api):
    """A 0-page PDF must fail loudly (→ ERROR, file stays for a human), never count as 'processed, 0 papers'."""
    import io
    from pypdf import PdfWriter
    from estamp.ocr_split import split_pdf
    calls, _ = split_api
    buf = io.BytesIO()
    PdfWriter().write(buf)
    with pytest.raises(RuntimeError, match="no pages"):
        split_pdf(buf.getvalue(), "empty.pdf", settings)
    assert calls == []


def test_split_api_passes_odd_last_part_through(settings, split_api):
    """The API does not reject an odd page count with splitSize=2 – it returns a 1-page last part.
    The adapter reports it faithfully; rejecting the upload (E106) is Phase B's job, before the call."""
    from estamp.ocr_split import split_pdf
    _, reply = split_api
    reply["resp"] = _Resp(200, (SPLIT_SAMPLES / "response_odd3_splitSize2.zip").read_bytes())
    parts = split_pdf((SPLIT_SAMPLES / "test_input_3pages.pdf").read_bytes(), "odd.pdf", settings, split_size=2)
    assert [(p.page_from, p.page_to) for p in parts] == [(1, 2), (3, 3)]


@e2e
def test_intake_via_split_api_keeps_front_and_back_together(env, monkeypatch):
    """Through the split API (fake that follows the captured contract): a scanned upload is sent with splitSize=2
    and each paper keeps its front + back."""
    import io, zipfile
    import estamp.ocr_split as ocr_split
    from estamp.cycle import run_cycle
    s, db, g = env
    monkeypatch.setitem(s.raw["intake"], "split_provider", "zoop_api")
    sizes = []

    def fake_api(url, files, data, timeout):
        sizes.append(data["splitSize"])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for p in ocr_split._split_local(files["file"][1], int(data["splitSize"])):
                z.writestr(f"-{p.page_from}-{p.page_to}.pdf", p.data)
        return _Resp(200, buf.getvalue())

    monkeypatch.setattr(ocr_split.requests, "post", fake_api)
    g.add("scan.pdf", PDF, _vendor_folder(g, s), samples.merged([
        samples.scanned_front("RAJASTHAN", "S 254201", 195), samples.scanned_back("RAJASTHAN"),
        samples.scanned_front("RAJASTHAN", "S 254202", 195), samples.scanned_back("RAJASTHAN")]))
    assert run_cycle(_ctx(s, db, g))
    assert sizes == ["2"]
    rows = db.q("SELECT cert_no, page_from, page_to, status FROM papers ORDER BY page_from")
    assert [(r["cert_no"], r["page_from"], r["page_to"], r["status"]) for r in rows] == [
        ("RJ-S254201", 1, 2, "AVAILABLE"), ("RJ-S254202", 3, 4, "AVAILABLE")], rows


@e2e
def test_traditional_page_with_a_text_layer_is_never_stocked_as_one_page(env):
    """A 'searchable' scan of a traditional paper next to an e-stamp: the kinds clash (2-page vs 1-page papers), so
    the whole file goes to Needs Review (E105) – the RJ front is never stocked as a 1-page paper without its back."""
    from estamp.cycle import run_cycle
    s, db, g = env
    g.add("searchable_scan.pdf", PDF, _vendor_folder(g, s), samples.merged([
        samples.paper_page(samples.cert("MH", 1), 200),
        samples.physical_front("RAJASTHAN", "S 254201", 195), samples.physical_back("RAJASTHAN")]))
    assert run_cycle(_ctx(s, db, g))
    status, notes, _ = _upload_review(db, g, s, "searchable_scan.pdf")
    assert status == "REVIEW" and "E105" in notes and "page 2" in notes, notes
    assert db.q1("SELECT count(*) n FROM papers")["n"] == 0


# ============================================================ OCR API fields → paper (pure, ADR-0006)
# Field values are made up, shaped like the real dev-API responses (structured fields only, no text).

def _f(**kv):
    """OCR API `fields`: every known field present, `{}` when not found, `{"value": …}` when found."""
    names = ["certification_no", "state_code", "state_name", "stamp_duty_amount", "unique_doc_reference",
             "first_party", "second_party"]
    return {n: ({"value": kv[n]} if n in kv else {}) for n in names}


@pytest.fixture(scope="session")
def rm(settings):
    r = StateResolver(settings.states)
    return r, CertMatcher(settings.get("intake.cert_patterns"), r)


@pytest.mark.parametrize("fields,expected", [
    (_f(certification_no="IN-HP90000000000001A", state_code="HP", stamp_duty_amount="100",
        unique_doc_reference="SUBIN-HPHP1904360426217599999999Y"), ("IN-HP90000000000001A", "HP", "estamp")),
    (_f(unique_doc_reference="SUBIN-UPUP1405480472263158599999Y", state_code="UP"),          # certificate unreadable
     ("SUBIN-UPUP1405480472263158599999Y", "UP", "estamp")),
    (_f(certification_no="DH 318899", state_code="MP", state_name="Madhya Pradesh"), ("MP-DH318899", "MP", "physical")),
    (_f(certification_no="g1k2026a999", state_name="HARYANA"), ("HR-G1K2026A999", "HR", "physical")),
    (_f(state_code="MP", state_name="Madhya Pradesh"), None),                                # a back page
    (_f(), None),
])
def test_id_from_api_fields(settings, rm, fields, expected):
    from estamp.extract import id_from_fields
    h = id_from_fields(fields, settings, rm[1], rm[0])
    assert (h and (h.cert, h.state, h.kind)) == expected


def test_analyze_fields_ok_and_failures(settings, rm):
    from estamp.extract import analyze_fields
    r, m = rm
    ok = analyze_fields(_f(certification_no="IN-HP90000000000001A", state_code="HP", stamp_duty_amount="100"),
                        settings, m, lambda c: False, r)
    assert (ok.cert, ok.state, ok.amount, ok.issues) == ("IN-HP90000000000001A", "HP", 100, [])
    bad_amount = analyze_fields(_f(certification_no="G1K2026A999", state_code="HR", stamp_duty_amount="101"),
                                settings, m, lambda c: False, r)
    assert any("₹101 is not an allowed amount for HR" in i for i in bad_amount.issues)
    no_amount = analyze_fields(_f(certification_no="DH 318899", state_code="MP"), settings, m, lambda c: False, r)
    assert "stamp duty amount not readable" in no_amount.issues
    none = analyze_fields(_f(state_code="MP"), settings, m, lambda c: False, r)
    assert "no certificate / serial number found" in none.issues
    garbage = analyze_fields(_f(certification_no="IN-XX12", state_code="MH", stamp_duty_amount="200"),
                             settings, m, lambda c: False, r)
    assert any("does not look like" in i for i in garbage.issues)
    dup = analyze_fields(_f(certification_no="DH 318899", state_code="MP", stamp_duty_amount="100"),
                         settings, m, lambda c: True, r)
    assert any("duplicate" in i for i in dup.issues)
    inactive = analyze_fields(_f(certification_no="KL 123456", state_code="KL", stamp_duty_amount="200"),
                              settings, m, lambda c: False, r)
    assert any("inactive" in i for i in inactive.issues)


# Ops' vendor sheet "Shriram Finanace daily orders" (2026-10-29) is the source of truth for state_config.csv:
# state → (stamp duty, qty for 10 days, pages per paper). Stamp type E → 1 page; T + S(ingle) → 1; T + D(ouble) → 2.
VENDOR_SHEET = {
    # AGREEMENT EXPRESS
    "RJ": (195, 6250, 1), "OD": (100, 2000, 2), "HP": (100, 1200, 1), "CH": (350, 1200, 1), "DL": (100, 3800, 1),
    "HR": (100, 3100, 1), "TN": (200, 8200, 1), "WB": (100, 3000, 2), "JH": (100, 1300, 1), "UP": (100, 18450, 1),
    "MP": (100, 7900, 2), "MH": (100, 30800, 1),
    # PAPER EASE
    "GJ": (300, 3650, 1), "KA": (500, 2800, 1), "CG": (50, 3750, 2), "PB": (350, 3300, 1), "JK": (350, 3300, 1),
    "KL": (200, 1750, 1), "SK": (100, 850, 2), "TR": (200, 850, 1), "BR": (1000, 4500, 1), "UK": (100, 1100, 1),
    "TS": (100, 3300, 1), "AP": (100, 4400, 1),
    # SABDA
    "AS": (100, 850, 1), "ML": (100, 850, 1), "MN": (100, 850, 1),
}


def test_state_config_matches_vendor_sheet():
    shipped = load_settings(ROOT / "config.yaml")             # not the session copy, which pins a test world
    got = {c: (st.amounts, st.daily_qty, st.pages_per_paper, st.active) for c, st in shipped.states.items()}
    assert got == {c: ([amt], qty10 // 10, pages, True) for c, (amt, qty10, pages) in VENDOR_SHEET.items()}


@pytest.mark.parametrize("cert,state,kind,pages", [
    ("IN-HP90000000000001A", "HP", "estamp", 1),     # e-stamps are always one page
    ("MP-DH318899", "MP", "physical", 2),            # traditional paper: front + back
    ("OD-123456", "OD", "physical", 2),
    ("HR-G1K2026A999", "HR", "physical", 1),         # state_config.csv: HR papers are one page
    ("MH-G1K2026A999", "MH", "physical", 1),         # MH stock is digital only, one page, no IN-… number
    ("XX-123456", "", "physical", 2),                # unknown state → the safe default
])
def test_pages_per_paper(settings, cert, state, kind, pages):
    from estamp.extract import IdHit, pages_per_paper
    assert pages_per_paper(IdHit(cert, state, state, kind), settings) == pages


# ============================================================ Phase B: pairing (pure rule)


@pytest.mark.parametrize("pairs,expected", [
    ([(1, 0), (1, 0), (1, 0)], None),             # every front has one serial, no back has one
    ([(1, 0), (0, 1), (1, 0)], 1),                # back page missing → the next front lands on the back side
    ([(0, 0), (1, 0)], 0),                        # break at the first pair
    ([(1, 0), (1, 1)], 1),                        # serial on a back page
    ([(1, 0), (2, 0)], 1),                        # two serials on one front
])
def test_first_broken_pair(pairs, expected):
    from estamp.extract import first_broken_pair
    assert first_broken_pair(pairs) == expected


def test_real_rajasthan_ocr_text(settings):
    """OCR text captured from the vendor's real scan (front + back of a physical RJ stamp paper)."""
    from estamp.extract import identify
    r = StateResolver(settings.states); m = CertMatcher(settings.get("intake.cert_patterns"), r)
    front = "INDIA NON JUDICIAL\nरु.1000\nONE THOUSAND RUPEES\nRs.1000\nराजस्थान RAJASTHAN\n24 SEP 2026\nVIONI\nS 254201"
    back = "245117\n24.09.2026\nKAABIL FINANCE PRIVATE LIMITED\nRAJASTHAN\nलाइसेन्स नं.- 28 / 2024\nरकम 1000/-"
    assert [h.cert for h in identify(normalize_ocr(front), settings, m, r)] == ["RJ-S254201"]
    assert identify(normalize_ocr(back), settings, m, r) == []
    a = analyze_paper(front + "\n" + back, "", settings, m, lambda c: False)
    assert (a.cert, a.state, a.amount) == ("RJ-S254201", "RJ", 1000)


@e2e
def test_physical_papers_front_and_back(env):
    """Scanned traditional upload (front + back pairs) – split in pairs, routed, issued, issue check passes.
    The last pair's serial is smudged: that pair breaks pairing and goes to Needs Review, still 2 pages."""
    from estamp.cycle import run_cycle
    s, db, g = env
    pages = []
    for serial in ["S 254201", "S 254202", "AB 100003", "(serial smudged)"]:
        pages += [samples.scanned_front("RAJASTHAN", serial, 195), samples.scanned_back("RAJASTHAN")]
    g.add("vendor_scan.pdf", PDF, _vendor_folder(g, s), samples.merged(pages))
    g.add("orders", GSHEET, s.folder("orders_incoming"),
          tabs={"x": [["LOAN NO", "STATE"], ["RJLAN0001", "RAJASTHAN"], ["RJLAN0002", "RAJASTHAN"]]})
    ctx = _ctx(s, db, g)
    assert run_cycle(ctx)
    smudged = db.q("SELECT * FROM papers WHERE cert_no IS NULL")
    assert len(smudged) == 1 and (smudged[0]["page_from"], smudged[0]["page_to"]) == (7, 8), smudged
    assert smudged[0]["status"] == "NEEDS_REVIEW" and "E107" in smudged[0]["notes"]
    papers = {r["cert_no"]: r for r in db.q("SELECT * FROM papers WHERE cert_no IS NOT NULL")}
    assert set(papers) == {"RJ-S254201", "RJ-S254202", "RJ-AB100003"}, set(papers)
    assert all(papers[k]["page_to"] - papers[k]["page_from"] == 1 for k in papers)
    done = db.q("SELECT o.lan, a.cert_no, a.verified_cert, a.pdf_file_id FROM orders o JOIN allocations a ON a.order_id=o.id")
    assert len(done) == 2 and all(d["verified_cert"] == d["cert_no"] for d in done), done
    from pypdf import PdfReader
    import io as _io
    assert all(len(PdfReader(_io.BytesIO(g.files[d["pdf_file_id"]]["data"])).pages) == 2 for d in done)
    left = [g.path_of(f["id"]) for f in g.files.values() if "inventory_root/RJ/195/" in g.path_of(f["id"])]
    assert len(left) == 1 and left[0].split("/")[-1] in {"RJ-S254201.pdf", "RJ-S254202.pdf", "RJ-AB100003.pdf"}, left


def _upload_review(db, g, s, name):
    """(upload status, upload notes, path of the original file) for a whole-upload Needs Review case."""
    up = db.q1("SELECT * FROM uploads WHERE file_name=%s", (name,))
    f = [f for f in g.files.values() if f["name"] == name][0]
    return up["status"], up["notes"] or "", g.path_of(f["id"])


@e2e
def test_mixed_upload_goes_whole_to_needs_review(env):
    """B-01: digital and scanned pages in one upload → whole file to Needs Review (E105), nothing split or stocked."""
    from estamp.cycle import run_cycle
    from fake_apis import FakeOcr, FakeSplitter
    s, db, g = env
    g.add("mixed.pdf", PDF, _vendor_folder(g, s), samples.merged([
        samples.paper_page(samples.cert("MH", 1), 200),
        samples.scanned_front("RAJASTHAN", "S 254201", 195), samples.scanned_back("RAJASTHAN")]))
    splitter = FakeSplitter()
    assert run_cycle(Ctx(s, db, g, ocr=FakeOcr(), splitter=splitter))
    status, notes, path = _upload_review(db, g, s, "mixed.pdf")
    assert status == "REVIEW" and "E105" in notes and path.startswith("needs_review/"), (status, notes, path)
    assert splitter.calls == [] and db.q1("SELECT count(*) n FROM papers")["n"] == 0


@e2e
def test_traditional_odd_page_count_goes_whole_to_review_before_split(env):
    """B-02: the split API would happily return a 1-page last part, so the odd count is caught BEFORE calling it."""
    from estamp.cycle import run_cycle
    from fake_apis import FakeOcr, FakeSplitter
    s, db, g = env
    g.add("odd.pdf", PDF, _vendor_folder(g, s), samples.merged([
        samples.scanned_front("RAJASTHAN", "S 254201", 195), samples.scanned_back("RAJASTHAN"),
        samples.scanned_front("RAJASTHAN", "S 254202", 195)]))
    splitter = FakeSplitter()
    assert run_cycle(Ctx(s, db, g, ocr=FakeOcr(), splitter=splitter))
    status, notes, path = _upload_review(db, g, s, "odd.pdf")
    assert status == "REVIEW" and "E106" in notes and path.startswith("needs_review/"), (status, notes, path)
    assert splitter.calls == [] and db.q1("SELECT count(*) n FROM papers")["n"] == 0


@e2e
def test_traditional_upload_is_split_in_pairs_and_each_page_ocrd(env):
    from estamp.cycle import run_cycle
    from fake_apis import FakeOcr, FakeSplitter
    s, db, g = env
    pages = []
    for serial in ["S 254201", "S 254202"]:
        pages += [samples.scanned_front("RAJASTHAN", serial, 195), samples.scanned_back("RAJASTHAN")]
    g.add("pairs.pdf", PDF, _vendor_folder(g, s), samples.merged(pages))
    ocr, splitter = FakeOcr(), FakeSplitter()
    assert run_cycle(Ctx(s, db, g, ocr=ocr, splitter=splitter))
    assert splitter.calls == [("pairs.pdf", 2)]
    assert sorted(ocr.calls) == ["pairs.pdf_p1", "pairs.pdf_p2", "pairs.pdf_p3", "pairs.pdf_p4"]
    rows = db.q("SELECT cert_no, page_from, page_to, status FROM papers ORDER BY page_from")
    assert [(r["cert_no"], r["page_from"], r["page_to"], r["status"]) for r in rows] == [
        ("RJ-S254201", 1, 2, "AVAILABLE"), ("RJ-S254202", 3, 4, "AVAILABLE")]


@e2e
def test_missing_back_page_sends_that_pair_and_all_later_pairs_to_review(env):
    """B-03: F1 B1 F2 F3 B3 F4 – F2's back is missing, so F3 lands on F2's back side. Pair 1 is fine and is stocked;
    from page 3 on every pair is suspect (E107), even (B3, F4) which a naive check might pass."""
    from estamp.cycle import run_cycle
    s, db, g = env
    f = lambda n: samples.scanned_front("RAJASTHAN", f"S 25420{n}", 195)
    b = lambda: samples.scanned_back("RAJASTHAN")
    g.add("broken.pdf", PDF, _vendor_folder(g, s), samples.merged([f(1), b(), f(2), f(3), b(), f(4)]))
    assert run_cycle(_ctx(s, db, g))
    rows = db.q("SELECT page_from, page_to, status, notes FROM papers ORDER BY page_from")
    assert [(r["page_from"], r["page_to"], r["status"]) for r in rows] == [
        (1, 2, "AVAILABLE"), (3, 4, "NEEDS_REVIEW"), (5, 6, "NEEDS_REVIEW")], rows
    assert all("E107" in r["notes"] and "page 3" in r["notes"] for r in rows[1:]), [r["notes"] for r in rows]


@e2e
def test_serial_the_api_returns_for_a_back_page_breaks_the_pairing(env):
    """The OCR API may return an ID directly (not in its text). One found on a back page means fronts have shifted."""
    from estamp.cycle import run_cycle
    from fake_apis import FakeOcr
    s, db, g = env
    pages = []
    for n in (1, 2):
        pages += [samples.scanned_front("RAJASTHAN", f"S 25420{n}", 195), samples.scanned_back("RAJASTHAN")]
    g.add("api_cert.pdf", PDF, _vendor_folder(g, s), samples.merged(pages))
    ocr = FakeOcr(certs={"api_cert.pdf_p4": "S 254209"})      # a serial on a back page
    assert run_cycle(Ctx(s, db, g, ocr=ocr))
    rows = db.q("SELECT page_from, status, notes FROM papers ORDER BY page_from")
    assert [(r["page_from"], r["status"]) for r in rows] == [(1, "AVAILABLE"), (3, "NEEDS_REVIEW")], rows
    assert "E107" in rows[1]["notes"]


@e2e
def test_traditional_parts_must_be_two_pages(env):
    """A split that ignores splitSize (4-page parts) must not stock paper 2's front inside paper 1."""
    from estamp.cycle import run_cycle
    from fake_apis import FakeOcr, FakeSplitter
    s, db, g = env
    pages = []
    for n in (1, 2):
        pages += [samples.scanned_front("RAJASTHAN", f"S 25420{n}", 195), samples.scanned_back("RAJASTHAN")]
    g.add("bad_split.pdf", PDF, _vendor_folder(g, s), samples.merged(pages))
    ctx = Ctx(s, db, g, ocr=FakeOcr(), splitter=FakeSplitter(force_size=4))
    run_cycle(ctx)
    assert db.q1("SELECT count(*) n FROM papers")["n"] == 0
    assert any(f["name"] == "bad_split.pdf" for f in _inbox_files(g, s))   # retried later
    assert any("not 2 page(s) each" in a for a in ctx.alerts), ctx.alerts


@e2e
def test_digital_page_with_two_ids_goes_alone_to_review(env):
    from estamp.cycle import run_cycle
    s, db, g = env
    two = f"{samples.cert('MH', 1)} {samples.cert('MH', 2)}"
    g.add("two_ids.pdf", PDF, _vendor_folder(g, s), samples.merged([
        samples.paper_page(two, 200), samples.paper_page(samples.cert("MH", 3), 200)]))
    assert run_cycle(_ctx(s, db, g))
    rows = db.q("SELECT page_from, status, notes FROM papers ORDER BY page_from")
    assert [(r["page_from"], r["status"]) for r in rows] == [(1, "NEEDS_REVIEW"), (2, "AVAILABLE")]
    assert "more than one" in rows[0]["notes"]


@e2e
def test_crash_mid_upload_then_rerun_stocks_nothing_twice(env, monkeypatch):
    """Crash while stocking pair 2 of 3: pair 1 is in the ledger, the file stays in the inbox. The next cycle finishes
    the file without stocking pair 1 again."""
    import estamp.intake as intake
    from estamp.cycle import run_cycle
    s, db, g = env
    pages = []
    for n in (1, 2, 3):
        pages += [samples.scanned_front("RAJASTHAN", f"S 25420{n}", 195), samples.scanned_back("RAJASTHAN")]
    g.add("crash.pdf", PDF, _vendor_folder(g, s), samples.merged(pages))
    real = intake._stock_unit

    def crashing(ctx, up, name, u):
        if u.page_from == 3:
            raise RuntimeError("simulated crash")
        return real(ctx, up, name, u)

    monkeypatch.setattr(intake, "_stock_unit", crashing)
    run_cycle(_ctx(s, db, g))
    assert db.q1("SELECT count(*) n FROM papers WHERE page_from=3")["n"] == 0
    assert any(f["name"] == "crash.pdf" for f in _inbox_files(g, s))
    monkeypatch.setattr(intake, "_stock_unit", real)
    assert run_cycle(_ctx(s, db, g))
    rows = db.q("SELECT cert_no, page_from FROM papers ORDER BY page_from")
    assert [(r["cert_no"], r["page_from"]) for r in rows] == [("RJ-S254201", 1), ("RJ-S254202", 3), ("RJ-S254203", 5)]
    assert not _inbox_files(g, s)


# ------------------------------------------------------------ OCR API path (A-05 + ADR-0006), fake API from fields

def _hp(n):
    return samples.api_page(certification_no=f"IN-HP9000000000000{n}A", state_code="HP", state_name="HIMACHAL PRADESH",
                            stamp_duty_amount="100", unique_doc_reference=f"SUBIN-HPHP19043604262175999999{n}Y")


def _mp_front(serial):
    return samples.api_page(certification_no=serial, state_code="MP", state_name="Madhya Pradesh", stamp_duty_amount="100")


def _mp_back():
    return samples.api_page(state_code="MP", state_name="Madhya Pradesh")      # 211: no certificate on a back


@e2e
def test_api_estamp_without_text_layer_is_stocked_and_issued(env):
    """Real e-stamps are image PDFs (no text layer). Read through the API's fields: 1 page per paper, raw response
    stored, and the issue check passes from the fields too."""
    from estamp.cycle import run_cycle
    from fake_apis import FakeApiOcr
    s, db, g = env
    g.add("hp.pdf", PDF, _vendor_folder(g, s), samples.merged([_hp(1), _hp(2)]))
    g.add("orders", GSHEET, s.folder("orders_incoming"), tabs={"x": [samples.H, ["1", "LAN-HP1", "c", "b", "HP", "100"]]})
    ocr = FakeApiOcr()
    assert run_cycle(Ctx(s, db, g, ocr=ocr))
    rows = db.q("SELECT cert_no, page_from, page_to, status, ocr_raw FROM papers ORDER BY page_from")
    assert [(r["cert_no"], r["page_from"], r["page_to"]) for r in rows] == [
        ("IN-HP90000000000001A", 1, 1), ("IN-HP90000000000002A", 2, 2)]
    assert rows[0]["ocr_raw"]["fields"]["certification_no"]["value"] == "IN-HP90000000000001A"
    a = db.q1("SELECT * FROM allocations")
    assert a["lan"] == "LAN-HP1" and a["verified_cert"] == a["cert_no"] == "IN-HP90000000000001A"
    assert "LAN-HP1.pdf_p1" in ocr.calls                                              # the issue check read it


@e2e
def test_api_traditional_pairs_and_one_page_state(env):
    """MP papers are front + back (default 2); HR papers are 1 page (state_config.csv pages_per_paper=1)."""
    from estamp.cycle import run_cycle
    from fake_apis import FakeApiOcr
    s, db, g = env
    g.add("mp.pdf", PDF, _vendor_folder(g, s),
          samples.merged([_mp_front("DH 318891"), _mp_back(), _mp_front("DH 318892"), _mp_back()]))
    g.add("hr.pdf", PDF, _vendor_folder(g, s), samples.merged([
        samples.api_page(certification_no=f"G1K2026A99{n}", state_code="HR", stamp_duty_amount="100") for n in (1, 2, 3)]))
    assert run_cycle(Ctx(s, db, g, ocr=FakeApiOcr()))
    got = {r["cert_no"]: (r["page_from"], r["page_to"], r["status"]) for r in db.q("SELECT * FROM papers")}
    assert got == {"MP-DH318891": (1, 2, "AVAILABLE"), "MP-DH318892": (3, 4, "AVAILABLE"),
                   "HR-G1K2026A991": (1, 1, "AVAILABLE"), "HR-G1K2026A992": (2, 2, "AVAILABLE"),
                   "HR-G1K2026A993": (3, 3, "AVAILABLE")}, got


@e2e
def test_api_file_whose_pages_disagree_goes_whole_to_review(env):
    """An e-stamp (1 page) and a traditional MP paper (2 pages) in one file → E105, nothing split or stocked."""
    from estamp.cycle import run_cycle
    from fake_apis import FakeApiOcr, FakeSplitter
    s, db, g = env
    g.add("mixed_api.pdf", PDF, _vendor_folder(g, s), samples.merged([_hp(1), _mp_front("DH 318891"), _mp_back()]))
    splitter = FakeSplitter()
    assert run_cycle(Ctx(s, db, g, ocr=FakeApiOcr(), splitter=splitter))
    status, notes, path = _upload_review(db, g, s, "mixed_api.pdf")
    assert status == "REVIEW" and "E105" in notes and path.startswith("needs_review/"), (status, notes)
    assert splitter.calls == [] and db.q1("SELECT count(*) n FROM papers")["n"] == 0


@e2e
def test_api_upload_with_no_readable_id_at_all_goes_whole_to_review(env):
    from estamp.cycle import run_cycle
    from fake_apis import FakeApiOcr
    s, db, g = env
    g.add("blank.pdf", PDF, _vendor_folder(g, s), samples.merged([_mp_back(), _mp_back()]))
    assert run_cycle(Ctx(s, db, g, ocr=FakeApiOcr()))
    status, notes, _ = _upload_review(db, g, s, "blank.pdf")
    assert status == "REVIEW" and "E105" in notes


@e2e
def test_api_page_1_ocr_failure_retries_the_whole_upload(env):
    """The type can't be decided without page 1 → the file stays in the inbox and is retried next cycle."""
    from estamp.cycle import run_cycle
    from fake_apis import FakeApiOcr
    s, db, g = env
    g.add("hp_fail.pdf", PDF, _vendor_folder(g, s), samples.merged([_hp(1), _hp(2)]))
    ctx = Ctx(s, db, g, ocr=FakeApiOcr(fail={"hp_fail.pdf_p1": RuntimeError("OCR API HTTP 503: down")}))
    run_cycle(ctx)
    assert db.q1("SELECT count(*) n FROM papers")["n"] == 0
    assert any(f["name"] == "hp_fail.pdf" for f in _inbox_files(g, s))
    assert run_cycle(Ctx(s, db, g, ocr=FakeApiOcr()))
    assert db.q1("SELECT count(*) n FROM papers WHERE status='AVAILABLE'")["n"] == 2


@e2e
def test_api_recheck_review_uses_the_stored_fields(env, monkeypatch):
    """HR ₹101 isn't allowed → Needs Review. After the config allows 101, recheck-review stocks it from the stored
    API reading – no new OCR call."""
    from estamp.actions import recheck_review
    from estamp.cycle import run_cycle
    from fake_apis import FakeApiOcr
    s, db, g = env
    g.add("hr101.pdf", PDF, _vendor_folder(g, s),
          samples.api_page(certification_no="G1K2026A991", state_code="HR", stamp_duty_amount="101"))
    assert run_cycle(Ctx(s, db, g, ocr=FakeApiOcr()))
    p = db.q1("SELECT * FROM papers")
    assert p["status"] == "NEEDS_REVIEW" and "₹101" in p["notes"]
    monkeypatch.setattr(s.states["HR"], "amounts", [100, 101])
    ocr = FakeApiOcr()
    moved, still = recheck_review(Ctx(s, db, g, ocr=ocr))
    assert (moved, still) == (1, 0) and ocr.calls == []
    assert db.q1("SELECT * FROM papers")["status"] == "AVAILABLE"


@e2e
def test_estamps_never_grouped(env):
    """E-stamps whose certificates are all unreadable: the kind (1 or 2 pages) can't be decided, so the whole file
    goes to Needs Review (E105) – pages are never guessed into pairs."""
    from estamp.cycle import run_cycle
    s, db, g = env
    g.add("ka_estamps.pdf", PDF, _vendor_folder(g, s), samples.merged([samples.estamp_no_cert() for _ in range(4)]))
    assert run_cycle(_ctx(s, db, g))
    status, notes, _ = _upload_review(db, g, s, "ka_estamps.pdf")
    assert status == "REVIEW" and "E105" in notes
    assert db.q1("SELECT count(*) n FROM papers")["n"] == 0


def test_configured_reader_is_the_ocr_api_only(settings, ocr_api):
    """D1 / ADR-0002 with config.yaml as shipped: even a PDF with a text layer is read by the OCR API, and an API
    failure is raised – never silently answered from the text layer or Drive OCR (no fallback path exists)."""
    from estamp.ocr_split import read_paper
    _, reply = ocr_api
    settings = load_settings(ROOT / "config.yaml")             # as shipped, not the session copy tests adjust
    assert settings.get("intake.ocr_provider") == "zoop_api"

    reply["resp"] = _ocr_resp(_f(certification_no="IN-MH90000000000001A", state_code="MH"))
    r = read_paper(samples.paper_page(samples.cert("MH", 1), 200), "x", settings)
    assert r.via == "zoop_api" and r.fields["certification_no"]["value"] == "IN-MH90000000000001A"
    reply["resp"] = _Resp(500, b'{"success":false,"message":"down"}', "application/json")
    with pytest.raises(RuntimeError, match="OCR API HTTP 500"):
        read_paper(samples.paper_page(samples.cert("MH", 1), 200), "x", settings)


UP_P2 = """INDIA NON JUDICIAL Government of Uttar Pradesh\ne-Stamp\n100 100 100 100\n\\*0000000100\nCertificate No.\nCertificate Issued Date Account Reference.\nUnique Doc. Reference\nPurchased by\nStamp Duty Paid By\n23-Sep-2026 06:38 PM\nNEWIMPACC (SV)/ up14054804/ GAUTAMBUDDH NAGAR 2/ UP-GBN\n:SUBIN-UPUP1405480472263158537115Y\nSHRIRAM FINANCE LIMITED\nStamp Duty Amount(Rs.)\n100\n(One Hundred only)\n"""
UP_P3 = """INDIA NON JUDICIAL Government of Uttar Pradesh\ne-Stamp\nCertificate No.\nStamp Duty Paid By\nStamp Duty Amount (Rs.)\n23-Sep-2026 06:39 PM\nNEWIMPACC (SV)/ up14054804/ GAUTAMBUDDH NAGAR 2/ UP-GBN\n:SUBIN-UPUP1405480472263355590018Y\nSHRIRAM FINANCE LIMITED\n100\n(One Hundred only)\n"""


KA_P1 = """INDIA NON JUDICIAL\nGovernment of Karnataka\ne-Stamp\nSHRIRAM FINANCE LIMITED SHRIRAM FINANCE\nSHRI Certificate No. RIRAM FINANCE LIMITED SHRIRAM FIN\nEDISH Unique Doc. Reference NCEL\n: 23-Sep-2026 08:53 PM : NONACC (FI)/ kakscsa08/ KORAMANGALA17/ KA-JYTED SHRIRAM FINANCE\n: SUBIN-KAKAKSCSA0817022928679844Y SHRIRAM FINANCE LIMITED SHRIRAM FINAN\nArticle 5(J) Agreement (in any other cases)\nDEED OF HYPOTHECATION\n: 0\n(Zero)\nConsideration Price (Rs.)\nStamp Duty Paid By FINANCE LIMITED SHRIRAM: ANGEL Stamp Duty Amount (Rs.) RAM FINANCE LIMITE: 500 FINANCE LIMITED SHRIRAM\n"""


@pytest.mark.parametrize("text,ref,state,amount", [
    (UP_P2, "SUBIN-UPUP1405480472263158537115Y", "UP", 100),
    (UP_P3, "SUBIN-UPUP1405480472263355590018Y", "UP", 100),
    (KA_P1, "SUBIN-KAKAKSCSA0817022928679844Y", "KA", 500),   # office code in the ref; watermark inside the amount line
])
def test_real_estamp_text(settings, text, ref, state, amount):
    """Real e-stamp OCR text: certificate no. not readable → Unique Doc. Reference used."""
    r = StateResolver(settings.states); m = CertMatcher(settings.get("intake.cert_patterns"), r)
    a = analyze_paper(text, "", settings, m, lambda c: False)
    assert (a.cert, a.state, a.amount, a.issues) == (ref, state, amount, [])


@pytest.mark.parametrize("words,n", [("One Hundred", 100), ("One Hundred Ninety Five", 195), ("One Thousand", 1000),
                                     ("Three Hundred Fifty", 350), ("Fifty", 50), ("One Lakh Twenty Thousand", 120000)])
def test_words_to_number(words, n):
    from estamp.extract import words_to_number
    assert words_to_number(words) == n


@pytest.mark.parametrize("variant", [
    "SUBIN-UPUP1405480472259151020965Y\nStamp Duty Amount (Rs.)\n23-Sep-2026\nSHRIRAM\n(One Hundred only)\nINDIA NON JUDICIAL",
    "\\*0000000100\nSUBIN-UPUP1405480472259151020965Y\nStamp Duty Amount (Rs.)\nSHRIRAM FINANCE LIMITED",
])
def test_up_estamp_amount_layout_variants(settings, variant):
    """Amount found even when the number isn't next to its label (words, or the *0000000100 code)."""
    r = StateResolver(settings.states); m = CertMatcher(settings.get("intake.cert_patterns"), r)
    a = analyze_paper(variant, "", settings, m, lambda c: False)
    assert (a.state, a.amount, a.issues) == ("UP", 100, [])


# ============================================================ reset-test-data (F-03)

@e2e
def test_reset_all_data_clears_tables_and_restarts_identity(env):
    s, db, g = env
    # seed a couple of rows across tables that use BIGSERIAL identities
    db.q("INSERT INTO uploads(drive_file_id, file_name, md5) VALUES ('f1','a.pdf','m1'),('f2','b.pdf','m2')")
    db.event("INFO", "seed", "hello")
    assert db.table_counts()["uploads"] == 2
    assert db.table_counts()["events"] == 1

    cleared = db.reset_all_data()
    assert cleared["uploads"] == 2 and cleared["events"] == 1

    # every table empty now
    assert all(n == 0 for n in db.table_counts().values())
    # identity restarted: next insert gets id 1 again
    new_id = db.q1("INSERT INTO uploads(drive_file_id) VALUES ('f3') RETURNING id")["id"]
    assert new_id == 1


@e2e
def test_cli_reset_refuses_production_schema_without_force(env, monkeypatch):
    import io
    from contextlib import redirect_stdout
    from estamp import cli
    s, db, g = env
    # seed one row we can prove survives
    db.q("INSERT INTO uploads(drive_file_id) VALUES ('keep')")

    # Point the CLI at the SAME test DB but a prod-looking schema name, so the guard sees production.
    # Give the CLI its own DB handle (it calls db.close() in finally) so the fixture's pool stays open.
    from estamp.db import DB
    def fake_build(args, need_google=True):
        s.raw["db"]["schema"] = "shriram_estamp"   # looks like prod to the guard
        return s, DB(DB_URL, "shriram_estamp", max_size=2), None
    monkeypatch.setattr(cli, "_build", fake_build)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["reset-test-data", "--yes"])   # --yes, but NO --force
    out = buf.getvalue()
    assert rc != 0
    assert "refus" in out.lower()
    # nothing was truncated
    assert db.q1("SELECT count(*) n FROM uploads")["n"] == 1


# ------------------------------------------------------------ E-07: ops-correction audit + RE-OCR + Corrections tab

def _needs_review_paper(db, g, s, drive_data, cert, state, amount, notes):
    """A NEEDS_REVIEW paper backed by a real stored PDF in Needs Review, ready for APPROVE / RE-OCR."""
    fid = g.add("review.pdf", PDF, s.folder("needs_review"), drive_data)
    return db.insert_paper(drive_file_id=fid, file_name="review.pdf", cert_no=cert, state=state,
                           denomination=amount, status="NEEDS_REVIEW", notes=notes)


def _review_result(ctx, s, g, pid, row_after_action):
    """Append a Review Actions row and run actions; return (result string, paper row)."""
    from estamp.actions import run_actions
    sid = s.get("control_sheet_id")
    g.ensure_tabs(sid, {"Review Actions": ["Paper ID", "Action", "Cert No", "State", "Stamp Duty", "Edited By",
                                           "Result", "Processed At"]})
    g.sheets_data[sid]["Review Actions"].append([str(pid)] + row_after_action)
    run_actions(ctx)
    res = g.sheets_data[sid]["Review Actions"][-1]
    return res, ctx.db.q1("SELECT * FROM papers WHERE id=%s", (pid,))


@e2e
def test_approve_with_correction_records_audit_and_still_approves(env):
    s, db, g = env
    pid = _needs_review_paper(db, g, s, samples.paper_page(samples.cert("MH", 1), 200),
                              cert="", state="MH", amount=200, notes="no certificate / serial number found")
    ctx = _ctx(s, db, g)
    res, p = _review_result(ctx, s, g, pid, ["APPROVE", samples.cert("MH", 777), "MH", "200", "ravi"])
    assert res[6].startswith("DONE: approved"), res
    assert p["status"] == "AVAILABLE" and p["cert_no"] == samples.cert("MH", 777)
    # audit: pre-edit API value captured, ops identity recorded
    assert p["api_cert_no"] == "" and p["api_state"] == "MH" and p["api_denomination"] == 200
    assert p["ops_edited_by"] == "ravi" and p["ops_edited_at"] is not None
    # approval behaviour preserved: file moved into Inventory
    assert g.path_of(p["drive_file_id"]).startswith("inventory_root/MH/200/")


@e2e
def test_approve_without_correction_records_no_audit(env):
    s, db, g = env
    cert = samples.cert("MH", 5)
    pid = _needs_review_paper(db, g, s, samples.paper_page(cert, 200),
                              cert=cert, state="MH", amount=200, notes="duplicate certificate – held")
    ctx = _ctx(s, db, g)
    res, p = _review_result(ctx, s, g, pid, ["APPROVE", "", "", "", ""])
    assert res[6].startswith("DONE: approved"), res
    assert p["status"] == "AVAILABLE"
    assert p["ops_edited_at"] is None and p["ops_edited_by"] is None and p["api_cert_no"] is None


@e2e
def test_re_ocr_rereads_and_moves_a_now_passing_paper_to_inventory(env):
    from fake_apis import FakeApiOcr
    s, db, g = env
    # stored PDF reads cleanly through the API, but the paper sits in Needs Review (e.g. an earlier bad reading)
    good = samples.merged([samples.api_page(certification_no="IN-HP90000000000009A", state_code="HP",
                                            state_name="HIMACHAL PRADESH", stamp_duty_amount="100")])
    pid = _needs_review_paper(db, g, s, good, cert="", state="", amount=0, notes="no certificate / serial number found")
    ocr = FakeApiOcr()
    ctx = Ctx(s, db, g, ocr=ocr)
    res, p = _review_result(ctx, s, g, pid, ["RE-OCR", "", "", "", ""])
    assert res[6].startswith("DONE: re-OCR'd →"), res
    assert p["status"] == "AVAILABLE" and p["cert_no"] == "IN-HP90000000000009A"
    assert g.path_of(p["drive_file_id"]).startswith("inventory_root/HP/100/")
    assert ocr.calls, "RE-OCR must make a new OCR call"


@e2e
def test_corrections_tab_lists_an_edited_paper(env):
    from estamp.dashboard import run_dashboard
    s, db, g = env
    pid = _needs_review_paper(db, g, s, samples.paper_page(samples.cert("MH", 2), 200),
                              cert="", state="MH", amount=200, notes="no certificate / serial number found")
    ctx = _ctx(s, db, g)
    ops_cert = samples.cert("MH", 778)
    _review_result(ctx, s, g, pid, ["APPROVE", ops_cert, "MH", "200", "ravi"])
    run_dashboard(ctx)
    tab = g.sheets_data[s.get("control_sheet_id")]["Corrections"]
    assert tab[0][:4] == ["Paper ID", "API read (cert/state/₹)", "Ops value (cert/state/₹)", "Edited By"]
    row = [r for r in tab[1:] if r[0] == pid][0]
    assert ops_cert in row[2] and row[3] == "ravi" and row[4]


# ------------------------------------------------------------ E-04: Manual Issues tab (system-down fallback)

def _manual_issue_result(ctx, s, g, row_after_lan):
    """Append a Manual Issues row (LAN | Cert | Issued By) and run actions; return the whole result row."""
    from estamp.actions import MANUAL_HEADER, MANUAL_TAB, run_actions
    sid = s.get("control_sheet_id")
    g.ensure_tabs(sid, {MANUAL_TAB: MANUAL_HEADER})
    g.sheets_data[sid][MANUAL_TAB].append(row_after_lan)
    run_actions(ctx)
    return g.sheets_data[sid][MANUAL_TAB][-1]


@e2e
def test_manual_issue_records_allocation_and_reaches_output_sheet(env):
    from estamp.outputs import run_outputs
    s, db, g = env
    cert = samples.cert("MH", 900)
    db.insert_orders([("LAN-MAN-1", "LAN-MAN-1", "MH", 200, "f", "fid", "t", 1, "PENDING", None)])
    o = db.q1("SELECT * FROM orders WHERE lan='LAN-MAN-1'")
    pid = db.insert_paper(state="MH", denomination=200, status="AVAILABLE", cert_no=cert)
    ctx = _ctx(s, db, g)
    row = _manual_issue_result(ctx, s, g, ["LAN-MAN-1", cert, "ravi", "", ""])
    assert row[3].startswith("DONE: recorded manual issue"), row
    assert row[4], "Processed At must be filled"
    assert db.q1("SELECT status FROM orders WHERE id=%s", (o["id"],))["status"] == "DONE"
    assert db.q1("SELECT status FROM papers WHERE id=%s", (pid,))["status"] == "ASSIGNED"
    a = db.q1("SELECT * FROM allocations WHERE order_id=%s", (o["id"],))
    assert a and a["paper_id"] == pid and a["cert_no"] == cert
    # reaches the Output sheet like an auto issue
    from estamp.allocate import label_for
    from estamp.outputs import output_sheet_id
    run_outputs(ctx)
    sid = output_sheet_id(ctx, a["alloc_date"], a["state"], a["denomination"])
    out = g.sheets_data[sid][label_for("MH", 200)]
    assert any(r[0] == "LAN-MAN-1" and r[1] == cert for r in out[1:]), out


@e2e
def test_manual_issue_for_already_done_lan_is_rejected_no_double_issue(env):
    from datetime import date
    s, db, g = env
    cert = samples.cert("MH", 901)
    db.insert_orders([("LAN-MAN-DONE", "LAN-MAN-DONE", "MH", 200, "f", "fid", "t", 1, "PENDING", None)])
    order = db.claim_orders("MH", 200, 1)[0]
    db.insert_paper(state="MH", denomination=200, status="AVAILABLE", cert_no="IN-MH-MANDONE")
    paper = db.reserve_paper(order)
    db.finish_allocation(order, paper, date(2026, 10, 28), "LAN-MAN-DONE.pdf", "fid", "link", "bid")
    # a free paper sits in stock; a manual issue for the already-DONE LAN must not consume it
    pid = db.insert_paper(state="MH", denomination=200, status="AVAILABLE", cert_no=cert)
    ctx = _ctx(s, db, g)
    row = _manual_issue_result(ctx, s, g, ["LAN-MAN-DONE", cert, "ravi", "", ""])
    assert row[3].startswith("ERROR: LAN not pending"), row
    assert db.q1("SELECT count(*) n FROM allocations WHERE lan='LAN-MAN-DONE'")["n"] == 1
    assert db.q1("SELECT status FROM papers WHERE id=%s", (pid,))["status"] == "AVAILABLE"


@e2e
def test_manual_issue_with_unavailable_or_mismatched_paper_is_rejected(env):
    s, db, g = env
    db.insert_orders([("LAN-MAN-2", "LAN-MAN-2", "MH", 200, "f", "fid", "t", 1, "PENDING", None)])
    o = db.q1("SELECT * FROM orders WHERE lan='LAN-MAN-2'")
    ctx = _ctx(s, db, g)
    # no such cert
    assert _manual_issue_result(ctx, s, g, ["LAN-MAN-2", "NOPE-1", "ravi", "", ""])[3].startswith("ERROR")
    # wrong denomination
    wrong = samples.cert("MH", 902)
    db.insert_paper(state="MH", denomination=100, status="AVAILABLE", cert_no=wrong)
    assert _manual_issue_result(ctx, s, g, ["LAN-MAN-2", wrong, "ravi", "", ""])[3].startswith("ERROR")
    # already assigned paper (not AVAILABLE)
    taken = samples.cert("MH", 903)
    db.insert_paper(state="MH", denomination=200, status="ASSIGNED", cert_no=taken)
    assert _manual_issue_result(ctx, s, g, ["LAN-MAN-2", taken, "ravi", "", ""])[3].startswith("ERROR")
    # no ledger change: order still PENDING, no allocation
    assert db.q1("SELECT status FROM orders WHERE id=%s", (o["id"],))["status"] == "PENDING"
    assert db.q1("SELECT count(*) n FROM allocations WHERE lan='LAN-MAN-2'")["n"] == 0


# ------------------------------------------------------------ E-06: automatic second OCR attempt before Needs Review

def _hp_fields(cert="IN-HP90000000000055A", amount="100"):
    from estamp.ocr_split import OcrResult
    return OcrResult("", "", "fake-api", raw={"fake": True},
                     fields={"certification_no": {"value": cert}, "state_code": {"value": "HP"},
                             "state_name": {"value": "HIMACHAL PRADESH"}, "stamp_duty_amount": {"value": amount}})


@e2e
def test_second_ocr_attempt_rescues_a_paper_that_failed_the_first_reading(env):
    """A paper whose type reads fine but whose FIRST reading fails a check (amount not allowed) is re-OCR'd ONCE; the
    better second reading passes, so it is STOCKED – exactly two OCR calls for that paper."""
    from estamp.cycle import run_cycle
    from fake_apis import ScriptedOcr
    s, db, g = env
    g.add("retry.pdf", PDF, _vendor_folder(g, s), samples.api_page(state_code="HP"))
    # 1st reading: valid HP cert (type decides) but a bad amount → fails checks; retry: clean HP ₹100.
    ocr = ScriptedOcr(script={"retry.pdf_p1": [_hp_fields(amount="999")]}, default=_hp_fields(amount="100"))
    assert run_cycle(Ctx(s, db, g, ocr=ocr))
    p = db.q1("SELECT * FROM papers")
    assert p["status"] == "AVAILABLE" and p["cert_no"] == "IN-HP90000000000055A" and p["denomination"] == 100, p
    # one read in _read_units + one retry in _stock_unit = two calls for this single paper
    assert len(ocr.calls) == 2, ocr.calls


@e2e
def test_paper_failing_both_ocr_attempts_goes_to_needs_review(env):
    """When the retry reading also fails the checks, the paper lands in Needs Review (coded E125: the amount read
    fine but is not allowed for the state) – the API output is still on the row, and no third attempt is made."""
    from estamp.cycle import run_cycle
    from fake_apis import ScriptedOcr
    s, db, g = env
    g.add("retry2.pdf", PDF, _vendor_folder(g, s), samples.api_page(state_code="HP"))
    ocr = ScriptedOcr(script={}, default=_hp_fields(amount="999"))   # valid cert, bad amount both times
    run_cycle(Ctx(s, db, g, ocr=ocr))
    p = db.q1("SELECT * FROM papers")
    assert p["status"] == "NEEDS_REVIEW" and "[E125]" in (p["notes"] or ""), p
    assert len(ocr.calls) == 2, ocr.calls


# ------------------------------------------------------------ E-08: issue check accepts the ops-corrected ID OR the API original

@e2e
def test_issue_check_accepts_api_original_cert_when_ops_corrected(env):
    """E-07 corrected the ledger cert_no; the physical PDF still prints the API's ORIGINAL cert. The issue check must
    accept EITHER value, so the LAN fulfils and the API original is recorded as the verifying cert."""
    from estamp.cycle import run_cycle
    from fake_apis import FakeApiOcr
    s, db, g = env
    api_cert, ledger_cert = "IN-MH90000000000010A", "IN-MH90000000000011A"   # PDF prints api_cert; ledger was corrected
    fid = g.add("p.pdf", PDF, s.folder("inventory_root"),
                samples.api_page(certification_no=api_cert, state_code="MH", stamp_duty_amount="200"))
    db.insert_paper(drive_file_id=fid, file_name="p.pdf", cert_no=ledger_cert, api_cert_no=api_cert,
                    state="MH", denomination=200, status="AVAILABLE")
    g.add("orders", GSHEET, s.folder("orders_incoming"),
          tabs={"x": [samples.H, ["1", "LAN-MH9", "c", "b", "MH", "200"]]})
    assert run_cycle(Ctx(s, db, g, ocr=FakeApiOcr()))
    a = db.q1("SELECT * FROM allocations")
    assert a and a["lan"] == "LAN-MH9" and a["cert_no"] == ledger_cert
    assert a["verified_cert"] == api_cert and a["verified_at"] is not None   # the value the PDF actually showed


@e2e
def test_issue_check_still_errors_when_pdf_matches_neither_value(env):
    """Safety: an issued PDF showing NEITHER the ledger cert NOR the stored api_cert still mismatches → ERROR."""
    from estamp.cycle import run_cycle
    from fake_apis import FakeApiOcr
    s, db, g = env
    fid = g.add("q.pdf", PDF, s.folder("inventory_root"),
                samples.api_page(certification_no="IN-MH90000000000099A", state_code="MH", stamp_duty_amount="200"))
    db.insert_paper(drive_file_id=fid, file_name="q.pdf", cert_no="IN-MH90000000000011A",
                    api_cert_no="IN-MH90000000000010A", state="MH", denomination=200, status="AVAILABLE")
    g.add("orders", GSHEET, s.folder("orders_incoming"),
          tabs={"x": [samples.H, ["1", "LAN-MHX", "c", "b", "MH", "200"]]})
    assert run_cycle(Ctx(s, db, g, ocr=FakeApiOcr()))
    assert db.q1("SELECT count(*) n FROM allocations")["n"] == 0
    assert db.q1("SELECT status FROM orders")["status"] == "ERROR"
    assert db.q1("SELECT status FROM papers")["status"] == "ERROR"


# ── #6 double-issue invariant + #8 atomic recovery (ledger safety) ──────────────────────
def _one_allocating_order(db, lan, cert):
    """Helper: a PENDING order claimed into ALLOCATING, with one AVAILABLE paper reserved to it."""
    db.insert_orders([(lan, lan.lower(), "MH", 200, "f", "fid", "t", 1, "PENDING", None)])
    order = db.claim_orders("MH", 200, 1)[0]
    db.insert_paper(state="MH", denomination=200, status="AVAILABLE", cert_no=cert)
    paper = db.reserve_paper(order)
    return order, paper


@e2e
def test_finish_allocation_wont_issue_the_same_order_twice(env):
    from datetime import date

    import pytest
    s, db, g = env
    order, paper = _one_allocating_order(db, "LAN-DUP-1", "IN-MH-DUP-1")
    db.finish_allocation(order, paper, date(2026, 10, 28), "LAN-DUP-1.pdf", "fid", "link", "bid")
    assert db.q1("SELECT status FROM orders WHERE id=%s", (order["id"],))["status"] == "DONE"
    assert db.q1("SELECT count(*) n FROM allocations WHERE order_id=%s", (order["id"],))["n"] == 1
    # the order is DONE now, not ALLOCATING – a second finish must refuse, never a 2nd issuance
    with pytest.raises(Exception):
        db.finish_allocation(order, paper, date(2026, 10, 28), "LAN-DUP-1.pdf", "fid2", "link2", "bid2")
    assert db.q1("SELECT count(*) n FROM allocations WHERE order_id=%s AND cancelled_at IS NULL",
                 (order["id"],))["n"] == 1


@e2e
def test_finish_allocation_refuses_an_order_not_in_allocating(env):
    from datetime import date

    import pytest
    s, db, g = env
    order, paper = _one_allocating_order(db, "LAN-GUARD-1", "IN-MH-GUARD-1")
    db.q("UPDATE orders SET status='ERROR' WHERE id=%s", (order["id"],))   # recovery/ops moved it out of ALLOCATING
    with pytest.raises(Exception):
        db.finish_allocation(order, paper, date(2026, 10, 28), "LAN-GUARD-1.pdf", "fid", "link", "bid")
    assert db.q1("SELECT status FROM orders WHERE id=%s", (order["id"],))["status"] == "ERROR"   # not flipped to DONE
    assert db.q1("SELECT count(*) n FROM allocations WHERE order_id=%s", (order["id"],))["n"] == 0


@e2e
def test_recover_stuck_flips_both_the_order_and_its_paper(env):
    s, db, g = env
    order, paper = _one_allocating_order(db, "LAN-STUCK-1", "IN-MH-STUCK-1")
    db.q("UPDATE orders SET updated_at = now() - interval '30 minutes' WHERE id=%s", (order["id"],))
    db.q("UPDATE papers SET updated_at = now() - interval '30 minutes' WHERE id=%s", (paper["id"],))
    n_o, n_p = db.recover_stuck(15)
    assert (n_o, n_p) == (1, 1)
    assert db.q1("SELECT status FROM orders WHERE id=%s", (order["id"],))["status"] == "ERROR"
    assert db.q1("SELECT status FROM papers WHERE id=%s", (paper["id"],))["status"] == "ERROR"


# ── #7 ops actions emit events + #11 CANCEL commits before the Drive move ───────────────
@e2e
def test_ops_actions_emit_events(env):
    """RETRY, KEEP_STATE and CANCEL each change ledger state – each must leave a row in the events table (Log tab)."""
    from estamp.cycle import run_cycle
    s, db, g = env
    g.add("TEST_Shriram_MH_LANs_26Oct", GSHEET, s.folder("orders_incoming"),
          tabs={"x": [samples.H, ["1", "LAN-CANCEL", "c", "b", "MH", "200"]]})
    _stock(g, s, "MH", 200, 1)
    assert run_cycle(_ctx(s, db, g))
    assert db.q1("SELECT status FROM orders WHERE lan='LAN-CANCEL'")["status"] == "DONE"

    db.q("INSERT INTO orders(lan, lan_key, state, denomination, status, notes) VALUES "
         "('LAN-RETRY','LAN-RETRY','MH',200,'ERROR','boom')")
    db.q("INSERT INTO orders(lan, lan_key, state, denomination, status) VALUES "
         "('LAN-CONF','LAN-CONF','MH',200,'CONFLICT'), ('LAN-CONF','LAN-CONF','KA',500,'CONFLICT')")

    before = db.q1("SELECT count(*) n FROM events")["n"]
    ctl = g.sheets_data[s.get("control_sheet_id")]
    ctl["Order Actions"].append(["LAN-RETRY", "RETRY", ""])
    ctl["Order Actions"].append(["LAN-CONF", "KEEP_STATE", "MH"])
    ctl["Order Actions"].append(["LAN-CANCEL", "CANCEL", ""])
    assert run_cycle(_ctx(s, db, g))

    results = [r[3] for r in ctl["Order Actions"][1:]]
    assert results == ["DONE: back to PENDING", "DONE: MH kept, others cancelled",
                       "DONE: cancelled, paper returned to stock"], results
    assert db.q1("SELECT count(*) n FROM events")["n"] > before
    for action, needle in [("Retry", "LAN-RETRY"), ("KeepState", "LAN-CONF"), ("Cancel", "LAN-CANCEL")]:
        hit = db.q1("SELECT count(*) n FROM events WHERE action=%s AND details LIKE %s", (action, f"%{needle}%"))
        assert hit["n"] >= 1, f"no event for {action} ({needle})"


@e2e
def test_cancel_after_issue_commits_before_drive_move(env):
    """CANCEL of a fulfilled LAN returns the paper to stock (AVAILABLE) and writes a CANCELLED output row.
    The ledger commit happens before the Drive move, so a move failure after commit leaves the ledger consistent."""
    from estamp.cycle import run_cycle
    s, db, g = env
    g.add("TEST_Shriram_MH_LANs_26Oct", GSHEET, s.folder("orders_incoming"),
          tabs={"x": [samples.H, ["1", "LAN-X", "c", "b", "MH", "200"]]})
    _stock(g, s, "MH", 200, 1)
    assert run_cycle(_ctx(s, db, g))
    o = db.q1("SELECT * FROM orders WHERE lan='LAN-X'")
    assert o["status"] == "DONE"

    ctl = g.sheets_data[s.get("control_sheet_id")]
    ctl["Order Actions"].append(["LAN-X", "CANCEL", ""])
    assert run_cycle(_ctx(s, db, g))

    assert ctl["Order Actions"][1][3] == "DONE: cancelled, paper returned to stock"
    p = db.q1("SELECT p.* FROM papers p WHERE p.id=%s", (o["paper_id"],))
    assert p["status"] == "AVAILABLE" and p["order_id"] is None
    assert g.path_of(p["drive_file_id"]).startswith("inventory_root/MH/200/")
    out = [rows for sid, tabs in g.sheets_data.items() if sid in g.files and g.files[sid]["name"] == "MH-200"
           for rows in tabs.values()][0]
    assert out[-1][5].startswith("CANCELLED"), out[-1]

    # move failure AFTER commit: ledger must still be consistent, and a WARN event is logged
    g.add("TEST_Shriram_MH_LANs_26Oct_2", GSHEET, s.folder("orders_incoming"),
          tabs={"x": [samples.H, ["1", "LAN-Y", "c", "b", "MH", "200"]]})
    _stock(g, s, "MH", 200, 1, start=50)
    assert run_cycle(_ctx(s, db, g))
    oy = db.q1("SELECT * FROM orders WHERE lan='LAN-Y'")
    assert oy["status"] == "DONE"

    boom = FakeGoogle.move_rename
    def fail_move(self, fid, new_parent, new_name=None):
        raise RuntimeError("Drive 503")
    FakeGoogle.move_rename = fail_move
    try:
        ctl["Order Actions"].append(["LAN-Y", "CANCEL", ""])
        warn_before = db.q1("SELECT count(*) n FROM events WHERE level='WARN'")["n"]
        assert run_cycle(_ctx(s, db, g))
    finally:
        FakeGoogle.move_rename = boom
    assert ctl["Order Actions"][-1][3] == "DONE: cancelled, paper returned to stock"
    py = db.q1("SELECT * FROM papers WHERE id=%s", (oy["paper_id"],))
    assert py["status"] == "AVAILABLE" and py["order_id"] is None
    assert db.q1("SELECT status FROM orders WHERE lan='LAN-Y'")["status"] == "CANCELLED"
    assert db.q1("SELECT count(*) n FROM events WHERE level='WARN'")["n"] > warn_before


# ── #10 wrap-up guard (lock never leaks) + Slack HTTP errors surfaced ────────────────────
def test_send_alerts_logs_when_slack_returns_http_error(settings, monkeypatch, caplog):
    # A Slack 4xx/5xx must surface in the log, not be silently swallowed (#10b).
    monkeypatch.setenv("ESTAMP_SLACK_WEBHOOK", "https://hooks.example/slack")

    class FakeResp:
        ok = False
        def raise_for_status(self):
            raise requests.HTTPError("500 Server Error")

    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp())
    ctx = Ctx(settings, db=None, google=None, ocr=object(), splitter=object())
    with caplog.at_level("ERROR"):
        ctx.send_alerts("subj", ["line"])
    assert any("Slack alert failed" in r.message for r in caplog.records), caplog.text


@e2e
def test_lock_released_even_if_send_raises(env):
    # If _send blows up in the finally block, the advisory lock must still be released and the run
    # must still be recorded (#10a) – otherwise a single alert failure wedges every future cycle.
    import estamp.cycle as _c
    from estamp.cycle import run_cycle
    s, db, g = env
    g.add("TEST_Shriram_MH_LANs_26Oct", GSHEET, s.folder("orders_incoming"), tabs={"x": samples.mh()})
    _stock(g, s, "MH", 200, 2)
    def boom(ctx):
        raise RuntimeError("boom in _send")
    orig = _c._send
    _c._send = boom
    try:
        run_cycle(_ctx(s, db, g))
    finally:
        _c._send = orig
    assert db.try_lock(), "advisory lock leaked after _send raised"
    db.release_lock()
    assert db.q1("SELECT count(*) n FROM runs WHERE finished_at IS NOT NULL")["n"] >= 1


# ── #9 intake crash between Drive upload and ledger insert must not duplicate the file ───
@e2e
def test_intake_crash_between_upload_and_insert_reuses_the_orphan(env, monkeypatch):
    from estamp.cycle import run_cycle
    s, db, g = env
    _stock(g, s, "MH", 200, 1)                       # one paper waiting in the inbox

    def boom(**f):
        raise RuntimeError("crash after Drive upload, before the ledger insert")
    monkeypatch.setattr(db, "insert_paper", boom)
    run_cycle(_ctx(s, db, g))                         # uploads the file, then the insert blows up → orphan
    monkeypatch.undo()

    # one orphan PDF now sits in Inventory with no ledger row
    inv = [f for f in g.files.values() if f["mimeType"] == PDF
           and g.path_of(f["id"]).startswith("inventory_root/MH/200/")]
    assert len(inv) == 1 and db.q1("SELECT count(*) n FROM papers")["n"] == 0

    run_cycle(_ctx(s, db, g))                         # re-run must adopt the orphan, not upload a second file
    inv = [f for f in g.files.values() if f["mimeType"] == PDF
           and g.path_of(f["id"]).startswith("inventory_root/MH/200/")]
    assert len(inv) == 1, [g.path_of(f["id"]) for f in inv]      # still exactly one physical file
    assert db.q1("SELECT count(*) n FROM papers WHERE status='AVAILABLE' AND state='MH'")["n"] == 1
    assert db.q1("SELECT drive_file_id FROM papers")["drive_file_id"] == inv[0]["id"]   # row points at it


# ── #4 alert dedup is hourly by default (a stuck condition keeps surfacing), daily when asked ──
@e2e
def test_alert_dedup_is_hourly_by_default_and_daily_when_asked(env):
    s, db, g = env
    ctx = _ctx(s, db, g)
    ctx.event("WARN", "X", "stuck", alert_key="failK")           # hourly (default)
    ctx.event("WARN", "X", "still stuck", alert_key="failK")     # same hour → deduped, no second alert
    assert len([a for a in ctx.alerts if a.startswith("WARN · X")]) == 1
    keys = [r["alert_key"] for r in db.q("SELECT alert_key FROM alerts_sent")]
    assert any(k.startswith("failK|") for k in keys), keys       # the hour is folded into the dedup key
    assert "failK" not in keys                                   # never the bare key when hourly

    ctx.event("WARN", "Y", "shortage", alert_key="shortK", dedup="day")   # daily opt-out
    keys = [r["alert_key"] for r in db.q("SELECT alert_key FROM alerts_sent")]
    assert "shortK" in keys, keys                                # stored bare: one a day, no hour bucket


# ── CANCEL marker: a failed output-sheet write must be loud, never a silently-lost "do not use" ──
@e2e
def test_cancel_sheet_marker_failure_is_loud_not_silent(env):
    from estamp.cycle import run_cycle
    s, db, g = env
    g.add("TEST_Shriram_MH_LANs_26Oct", GSHEET, s.folder("orders_incoming"),
          tabs={"x": [samples.H, ["1", "LAN-M", "c", "b", "MH", "200"]]})
    _stock(g, s, "MH", 200, 1)
    assert run_cycle(_ctx(s, db, g))
    o = db.q1("SELECT * FROM orders WHERE lan='LAN-M'")
    assert o["status"] == "DONE"

    ctl = g.sheets_data[s.get("control_sheet_id")]
    orig = FakeGoogle.append_rows
    def boom(self, sid, title, rows):   # only the CANCELLED marker append fails; everything else works
        if any(len(r) > 5 and "do not use" in str(r[5]) for r in rows):
            raise RuntimeError("Sheets 500")
        return orig(self, sid, title, rows)
    FakeGoogle.append_rows = boom
    try:
        ctl["Order Actions"].append(["LAN-M", "CANCEL", ""])
        err_before = db.q1("SELECT count(*) n FROM events WHERE level='ERROR'")["n"]
        assert run_cycle(_ctx(s, db, g))
    finally:
        FakeGoogle.append_rows = orig
    # cancellation still committed despite the failed sheet write
    assert ctl["Order Actions"][-1][3] == "DONE: cancelled, paper returned to stock"
    assert db.q1("SELECT status FROM papers WHERE id=%s", (o["paper_id"],))["status"] == "AVAILABLE"
    assert db.q1("SELECT status FROM orders WHERE lan='LAN-M'")["status"] == "CANCELLED"
    # and the missing marker was reported loudly, not silently dropped
    assert db.q1("SELECT count(*) n FROM events WHERE level='ERROR' AND action='Cancel' "
                 "AND details LIKE '%do not use%'")["n"] >= 1
    assert db.q1("SELECT count(*) n FROM events WHERE level='ERROR'")["n"] > err_before


# ------------------------------------------------------------ error codes on every intake path (catalogue wiring)

@pytest.mark.parametrize("issues,cert,code", [
    (["duplicate certificate IN-X – already in stock or used"], "IN-X", "E126"),
    (["more than one certificate / serial number on this paper: A, B"], "", "E121"),
    (["certificate state code 'ZZ' is not in state_config.csv"], "IN-ZZ1", "E122"),
    (["state of 'X1' is not in state_config.csv", "stamp duty amount not readable"], "X1", "E122"),
    (["no certificate / serial number found", "stamp duty amount not readable"], "", "E120"),
    (["stamp duty amount not readable"], "IN-X", "E124"),
    (["stamp duty ₹999 is not an allowed amount for HP [100]"], "IN-X", "E125"),
    (["state KL is marked inactive in state_config.csv (x)"], "IN-X", ""),
])
def test_review_code_picks_the_catalogue_code(issues, cert, code):
    from estamp.extract import PaperAnalysis, review_code
    assert review_code(PaperAnalysis(cert=cert, issues=issues)) == code


def _events(db, like):
    return [e["details"] for e in db.q("SELECT details FROM events WHERE details LIKE %s", (like,))]


@e2e
def test_paper_review_notes_carry_e121_e122_e126(env):
    from estamp.cycle import run_cycle
    s, db, g = env
    two = f"{samples.cert('MH', 1)} {samples.cert('MH', 2)}"
    g.add("codes.pdf", PDF, _vendor_folder(g, s), samples.merged([
        samples.paper_page(two, 200), samples.paper_page(samples.cert("ZZ", 3), 200),
        samples.paper_page(samples.cert("MH", 4), 200), samples.paper_page(samples.cert("MH", 4), 200)]))
    assert run_cycle(_ctx(s, db, g))
    notes = {r["page_from"]: r["notes"] or "" for r in db.q("SELECT page_from, notes FROM papers")}
    assert "[E121]" in notes[1] and "more than one" in notes[1], notes
    assert "[E122]" in notes[2] and "ZZ" in notes[2], notes
    dup = [notes[p] for p in (3, 4) if notes[p]]
    assert len(dup) == 1 and "[E126]" in dup[0] and "duplicate" in dup[0], notes
    assert _events(db, "%[E121]%") and _events(db, "%[E126]%")


@e2e
def test_pairing_break_note_keeps_its_e107_prefix(env):
    """E107 notes must start with 'E107' (recheck-review skips those) – not be re-tagged as E120 'no certificate'."""
    from estamp.cycle import run_cycle
    s, db, g = env
    g.add("pair.pdf", PDF, _vendor_folder(g, s), samples.merged([
        samples.scanned_front("RAJASTHAN", "S 254201", 195), samples.scanned_back("RAJASTHAN"),
        samples.scanned_front("RAJASTHAN", "(serial smudged)", 195), samples.scanned_back("RAJASTHAN")]))
    assert run_cycle(_ctx(s, db, g))
    p = db.q1("SELECT notes FROM papers WHERE page_from=3")
    assert p["notes"].startswith("E107") and "E120" not in p["notes"], p


@e2e
def test_ocr_failure_event_carries_e111(env):
    from estamp.cycle import run_cycle
    from fake_apis import FakeOcr
    s, db, g = env
    _stock(g, s, "MH", 200, 2)
    assert run_cycle(Ctx(s, db, g, ocr=FakeOcr(fail={"vendor_MH_200_1.pdf_p2": RuntimeError("OCR API HTTP 503")})))
    ev = _events(db, "%[E111]%")
    assert ev and "503" in ev[0] and "inbox" in ev[0], ev


@e2e
def test_split_failure_event_carries_e110(env):
    from estamp.cycle import run_cycle
    from fake_apis import FakeOcr

    class BrokenSplitter:
        def split(self, data, name, split_size=1):
            raise RuntimeError("split API HTTP 502: bad gateway")

    s, db, g = env
    _stock(g, s, "MH", 200, 2)
    assert run_cycle(Ctx(s, db, g, ocr=FakeOcr(), splitter=BrokenSplitter()))
    ev = _events(db, "%[E110]%")
    assert ev and "502" in ev[0], ev
    assert db.q1("SELECT count(*) n FROM papers")["n"] == 0


@e2e
def test_ocr_auth_failure_event_carries_e113(env):
    from estamp.cycle import run_cycle
    from fake_apis import FakeOcr
    s, db, g = env
    _stock(g, s, "MH", 200, 1)
    assert run_cycle(Ctx(s, db, g, ocr=FakeOcr(fail={"vendor_MH_200_1.pdf_p1": ApiAuthError("refused (HTTP 401)")})))
    ev = [e["details"] for e in db.q("SELECT details FROM events WHERE level='ERROR' AND details LIKE '%[E113]%'")]
    assert ev and "401" in ev[0], ev


# ------------------------------------------------------------ M-4: unreadable / oversized PDFs go whole to review, early

def _encrypted_pdf() -> bytes:
    import io as _io
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    for p in PdfReader(_io.BytesIO(samples.paper_page(samples.cert("MH", 1), 200))).pages:
        w.add_page(p)
    w.encrypt("secret")
    buf = _io.BytesIO()
    w.write(buf)
    return buf.getvalue()


@e2e
@pytest.mark.parametrize("data,needle", [(_encrypted_pdf(), "password-protected"),
                                         (b"%PDF-1.4\n not really a pdf", "can't be opened")], ids=["encrypted", "corrupt"])
def test_unreadable_pdf_goes_whole_to_review_with_e103(env, data, needle):
    from estamp.cycle import run_cycle
    from fake_apis import FakeOcr
    s, db, g = env
    g.add("locked.pdf", PDF, _vendor_folder(g, s), data)
    ocr = FakeOcr()
    assert run_cycle(Ctx(s, db, g, ocr=ocr))
    status, notes, path = _upload_review(db, g, s, "locked.pdf")
    assert status == "REVIEW" and "E103" in notes and needle in notes and path.startswith("needs_review/"), notes
    assert ocr.calls == [] and db.q1("SELECT count(*) n FROM papers")["n"] == 0
    assert _events(db, "%[E103]%")


@e2e
def test_oversized_upload_goes_whole_to_review_before_any_ocr_call(env, monkeypatch):
    from estamp.cycle import run_cycle
    from estamp.errors import CATALOGUE
    from fake_apis import FakeOcr
    s, db, g = env
    monkeypatch.setitem(s.raw["intake"], "max_upload_mb", 0.001)       # ~1 KB, smaller than any sample PDF
    _stock(g, s, "MH", 200, 2)
    ocr = FakeOcr()
    assert run_cycle(Ctx(s, db, g, ocr=ocr))
    status, notes, path = _upload_review(db, g, s, "vendor_MH_200_1.pdf")
    assert status == "REVIEW" and "E108" in notes and "MB" in notes and path.startswith("needs_review/"), notes
    assert ocr.calls == [] and "E108" in CATALOGUE


# ------------------------------------------------------------ L-2: a Drive file without modifiedTime

@e2e
def test_upload_without_modified_time_is_read_not_crashed(env):
    from estamp.cycle import run_cycle
    s, db, g = env
    up = _stock(g, s, "MH", 200, 2)
    del g.files[up]["modifiedTime"]
    assert run_cycle(_ctx(s, db, g))
    assert db.q1("SELECT count(*) n FROM papers WHERE status='AVAILABLE'")["n"] == 2
    assert len(_events(db, "%no modifiedTime%")) == 1


@e2e
def test_upload_without_modified_time_waits_out_the_settle_window_and_logs_once(env, monkeypatch):
    import estamp.intake as intake
    from datetime import datetime, timedelta, timezone
    from estamp.cycle import run_cycle
    s, db, g = env
    monkeypatch.setitem(s.raw["intake"], "settle_minutes", 5)
    up = _stock(g, s, "MH", 200, 1)
    del g.files[up]["modifiedTime"]
    for _ in range(2):
        assert run_cycle(_ctx(s, db, g))
        assert [f["id"] for f in _inbox_files(g, s)] == [up], "still settling"
    assert len(_events(db, "%no modifiedTime%")) == 1, "logged once, not every cycle"
    intake._first_seen[up] = datetime.now(timezone.utc) - timedelta(minutes=6)      # the window has passed
    assert run_cycle(_ctx(s, db, g))
    assert db.q1("SELECT count(*) n FROM papers WHERE status='AVAILABLE'")["n"] == 1


# ------------------------------------------------------------ intake deadline is checked while reading pages

@e2e
def test_page_reading_stops_at_the_intake_deadline_and_leaves_the_upload_untouched(env, monkeypatch):
    """A big upload must not hold the cycle past the intake budget: once the deadline passes no further page is
    read, nothing is stocked, and the upload stays in the inbox (PROCESSING) to be read again next cycle."""
    import types
    import estamp.intake as intake
    from fake_apis import FakeOcr
    s, db, g = env
    clock = [0.0]
    monkeypatch.setattr(intake, "time", types.SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setitem(s.raw, "workers", 1)                 # pages read in order, so the cut-off is exact
    up = _stock(g, s, "MH", 200, 5)

    class SlowOcr(FakeOcr):
        def read(self, data, name):
            if name.endswith("_p2"):
                clock[0] = 2000.0                            # reading page 2 runs past the deadline
            return super().read(data, name)

    ocr = SlowOcr()
    intake.run_intake(Ctx(s, db, g, ocr=ocr), deadline=1000.0)
    assert sorted(ocr.calls) == ["vendor_MH_200_1.pdf_p1", "vendor_MH_200_1.pdf_p2"], ocr.calls
    assert db.q1("SELECT count(*) n FROM papers")["n"] == 0
    assert [f["id"] for f in _inbox_files(g, s)] == [up]
    assert db.q1("SELECT status FROM uploads")["status"] == "PROCESSING"
    assert _events(db, "%time budget%2 of 5%"), _events(db, "%")
    assert db.q1("SELECT count(*) n FROM events WHERE level IN ('ERROR','WARN')")["n"] == 0

    clock[0] = 0.0                                            # next cycle, fresh budget: the whole file is read
    intake.run_intake(Ctx(s, db, g, ocr=FakeOcr()), deadline=1000.0)
    assert db.q1("SELECT count(*) n FROM papers WHERE status='AVAILABLE'")["n"] == 5
    assert not _inbox_files(g, s)


@e2e
def test_an_upload_that_never_fits_the_budget_gives_up_like_a_failing_read(env, monkeypatch):
    """No new retry loop: an upload cut off by the deadline for longer than the A-08 give-up window goes whole to
    Needs Review, the same as one whose OCR keeps failing."""
    import estamp.intake as intake
    from fake_apis import FakeOcr
    s, db, g = env
    _stock(g, s, "MH", 200, 2)
    intake.run_intake(Ctx(s, db, g, ocr=FakeOcr()), deadline=0.0)       # the deadline has already passed
    assert db.q1("SELECT count(*) n FROM uploads")["n"] == 0, "deadline before the first file: nothing touched"

    def cut_off(*a, **k):
        raise intake.OutOfTime("intake time budget ran out")
    monkeypatch.setattr(intake, "_read_units", cut_off)
    later = time.time() + 3600
    intake.run_intake(Ctx(s, db, g, ocr=FakeOcr()), deadline=later)
    assert db.q1("SELECT status FROM uploads")["status"] == "PROCESSING"
    db.q("UPDATE uploads SET created_at = now() - interval '31 minutes'")
    intake.run_intake(Ctx(s, db, g, ocr=FakeOcr()), deadline=later)
    assert db.q1("SELECT status FROM uploads")["status"] == "REVIEW"
    assert not _inbox_files(g, s)


# ------------------------------------------------------------ whole-file reviews show up in the cycle stats

@e2e
def test_whole_file_review_is_counted_in_the_cycle_summary(env):
    from estamp.cycle import run_cycle
    s, db, g = env
    g.add("odd.pdf", PDF, _vendor_folder(g, s), samples.merged([
        samples.scanned_front("RAJASTHAN", "S 254201", 195), samples.scanned_back("RAJASTHAN"),
        samples.scanned_front("RAJASTHAN", "S 254202", 195)]))
    ctx = _ctx(s, db, g)
    assert run_cycle(ctx)
    assert ctx.stats.uploads_review == 1 and ctx.stats.papers_review == 0
    assert db.q1("SELECT summary FROM runs")["summary"]["uploads_review"] == 1
    assert _events(db, "summary:%1 whole file%"), _events(db, "summary:%")


# ------------------------------------------------------------ L-4: OCR text fed to the regexes is capped

def test_normalize_ocr_caps_its_input_at_the_storage_cap():
    from estamp.extract import MAX_OCR_TEXT
    assert MAX_OCR_TEXT == 20000
    assert len(normalize_ocr("x" * (MAX_OCR_TEXT * 3))) == MAX_OCR_TEXT
    assert normalize_ocr("in-mh 1234") == "IN-MH1234"          # short text unchanged in behaviour


# ------------------------------------------------------------ H-5: the E-06 retry reading survives a crash

def _retry_calls(ocr):
    return [c for c in ocr.calls if c.endswith("#retry")]


@e2e
def test_crash_after_the_second_ocr_attempt_does_not_bill_it_again(env, monkeypatch):
    """Crash after _reread's OCR call but before insert_paper: the re-run adopts the stored retry reading instead of
    calling OCR a second time for the same paper – and still stocks it from that (good) reading."""
    from estamp.cycle import run_cycle
    from fake_apis import ScriptedOcr
    s, db, g = env
    g.add("retry_crash.pdf", PDF, _vendor_folder(g, s), samples.api_page(state_code="HP"))
    ocr = ScriptedOcr(script={"retry_crash.pdf_p1": [_hp_fields(amount="999")]}, default=_hp_fields(amount="100"))

    def boom(**f):
        raise RuntimeError("crash after the retry OCR, before the ledger insert")
    monkeypatch.setattr(db, "insert_paper", boom)
    run_cycle(Ctx(s, db, g, ocr=ocr))
    monkeypatch.undo()
    assert db.q1("SELECT count(*) n FROM papers")["n"] == 0
    assert any(f["name"] == "retry_crash.pdf" for f in _inbox_files(g, s))

    assert run_cycle(Ctx(s, db, g, ocr=ocr))
    assert len(_retry_calls(ocr)) == 1, ocr.calls
    p = db.q1("SELECT * FROM papers")
    assert p["status"] == "AVAILABLE" and p["denomination"] == 100, p


@e2e
def test_a_failed_second_ocr_attempt_is_not_repeated_after_a_crash(env, monkeypatch):
    from estamp.cycle import run_cycle
    from fake_apis import ScriptedOcr
    s, db, g = env
    g.add("retry_fail.pdf", PDF, _vendor_folder(g, s), samples.api_page(state_code="HP"))

    class FailingRetry(ScriptedOcr):
        def read(self, data, name):
            r = super().read(data, name)
            if name in _retry_calls(self):
                raise RuntimeError("OCR API HTTP 503")
            return r

    ocr = FailingRetry(script={}, default=_hp_fields(amount="999"))

    def boom(**f):
        raise RuntimeError("crash before the ledger insert")
    monkeypatch.setattr(db, "insert_paper", boom)
    run_cycle(Ctx(s, db, g, ocr=ocr))
    monkeypatch.undo()
    assert run_cycle(Ctx(s, db, g, ocr=ocr))
    assert len(_retry_calls(ocr)) == 1, ocr.calls
    assert db.q1("SELECT status FROM papers")["status"] == "NEEDS_REVIEW"


# ------------------------------------------------------------ whole-file review approved by file name

def _review_upload(db, g, s, name, data):
    """A whole-file review (E105) upload: the file sits in Needs Review, no paper row exists for it."""
    fid = g.add(name, PDF, s.folder("needs_review"), data)
    up = db.upload_get_or_create(fid, name, "")
    db.upload_finish(up["id"], "REVIEW", 0, "E105 no page has a readable certificate / serial")
    return fid


@e2e
def test_whole_file_review_is_approved_by_file_name_and_issued_without_ocr_verify(env):
    s, db, g = env
    fid = _review_upload(db, g, s, "page62.pdf", samples.paper_page("", 200))
    ctx = _ctx(s, db, g)
    from estamp.actions import run_actions
    sid = s.get("control_sheet_id")
    g.ensure_tabs(sid, {"Review Actions": ["Paper ID", "Action", "Cert No", "State", "Stamp Duty", "Edited By",
                                           "Result", "Processed At"]})
    g.sheets_data[sid]["Review Actions"].append(["page62.pdf", "APPROVE", "99AB 794915", "MH", "200", "sanket"])
    run_actions(ctx)
    res = g.sheets_data[sid]["Review Actions"][-1]
    assert res[6].startswith("DONE: approved"), res
    p = db.q1("SELECT * FROM papers WHERE drive_file_id=%s", (fid,))
    assert (p["cert_no"], p["state"], p["denomination"], p["status"]) == ("MH-99AB794915", "MH", 200, "AVAILABLE"), p
    assert p["ocr_via"] == "ops" and p["ops_edited_by"] == "sanket"
    assert g.path_of(fid).startswith("inventory_root/MH/200/")
    # second time: same name again is not approved twice
    g.sheets_data[sid]["Review Actions"].append(["page62.pdf", "APPROVE", "99AB 794915", "MH", "200", "sanket"])
    run_actions(ctx)
    assert g.sheets_data[sid]["Review Actions"][-1][6].startswith("ERROR"), g.sheets_data[sid]["Review Actions"][-1]
    # issue: the PDF has no readable cert, but an ops-entered paper is not OCR-verified – it is issued, not ERROR
    from estamp.cycle import run_cycle
    g.add("TEST_Shriram_MH_LANs", GSHEET, s.folder("orders_incoming"),
          tabs={"x": [["LOAN NO", "STATE", "STAMP DUTY"], ["TGHYDTW2610260001", "MH", "200"]]}, age_min=20)
    assert run_cycle(ctx)
    o = db.q1("SELECT status FROM orders WHERE lan_key='TGHYDTW2610260001'")
    assert o["status"] == "DONE", o


@e2e
def test_paper_approved_with_a_typed_cert_is_trusted_at_issue_others_are_verified(env):
    s, db, g = env
    typed = _needs_review_paper(db, g, s, samples.paper_page("", 200), cert="", state="MH", amount=200,
                                notes="no certificate / serial number found")
    plain = _needs_review_paper(db, g, s, samples.paper_page(samples.cert("MH", 6), 200), cert=samples.cert("MH", 6),
                                state="MH", amount=200, notes="duplicate certificate – held")
    ctx = _ctx(s, db, g)
    res, p = _review_result(ctx, s, g, typed, ["APPROVE", "99AB 794915", "MH", "200", "sanket"])
    assert res[6].startswith("DONE: approved"), res
    assert p["ocr_via"] == "ops", p                      # stamp over the number: the typed cert is used at issue
    res, p = _review_result(ctx, s, g, plain, ["APPROVE", "", "", "", ""])
    assert p["ocr_via"] != "ops", p                      # nothing typed: second OCR as usual


@e2e
def test_vendor_register_number_on_a_mp_back_page_does_not_break_the_pairing(env):
    """Real MP paper (2026-10-29): front DX 594001; the back carries the stamp vendor's register no. 1691735, which
    the OCR API returns as certification_no. Only a serial-shaped number on a back page breaks the pairing."""
    from estamp.cycle import run_cycle
    from fake_apis import FakeApiOcr
    s, db, g = env
    pages = []
    for serial, register in (("DX 594001", "1691735"), ("DX 594002", "1691736")):
        pages += [samples.api_page(certification_no=serial, state_code="MP", stamp_duty_amount="100"),
                  samples.api_page(certification_no=register, state_code="MP")]
    g.add("page185-186.pdf", PDF, _vendor_folder(g, s), samples.merged(pages))
    assert run_cycle(Ctx(s, db, g, ocr=FakeApiOcr()))
    rows = db.q("SELECT cert_no, page_from, page_to, status FROM papers ORDER BY page_from")
    assert [(r["cert_no"], r["page_from"], r["page_to"], r["status"]) for r in rows] == [
        ("MP-DX594001", 1, 2, "AVAILABLE"), ("MP-DX594002", 3, 4, "AVAILABLE")], rows
