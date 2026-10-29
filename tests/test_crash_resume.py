"""E-09 (HANDOFF §7 E-6): crash-resume guarantees, proven by tests.

A crash at each step of intake and allocation must leave the system correct on the next run: the LAN ends
issued exactly once OR is cleanly recoverable to ERROR/PENDING; the paper is never stocked or issued twice; the
output sheet holds no duplicate row.

A crash is simulated by monkeypatching the function called at the target step to raise *after* its Drive/DB side
effect has happened (so the next run sees the half-done world a real crash would leave). The patch is then dropped
and a fresh cycle runs; the invariants are asserted.

Setup replicates the e2e pattern in tests/test_all.py (schema bring-up, a Ctx over the in-memory fakes) using only
the importable modules – no private fixtures from test_all are imported.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from estamp.context import Ctx  # noqa: E402
from estamp.cycle import run_cycle  # noqa: E402
from estamp.google_clients import GSHEET, PDF  # noqa: E402
from estamp.settings import load_settings  # noqa: E402

import samples  # noqa: E402
from fake_apis import FakeOcr  # noqa: E402
from fake_google import FakeGoogle  # noqa: E402

DB_URL = os.environ.get("ESTAMP_TEST_DB_URL")
e2e = pytest.mark.skipif(not DB_URL, reason="set ESTAMP_TEST_DB_URL to run end-to-end tests")


# ---------------------------------------------------------------- local setup helpers (mirrors test_all.py)

@pytest.fixture
def env():
    import psycopg
    from estamp.db import DB
    settings = load_settings(ROOT / "config.yaml")
    settings.states["MH"].amounts = [200]      # these scenarios stock MH ₹200 (pre vendor sheet; shipped MH is ₹100)
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS estamp_test CASCADE")
    settings.raw["db"]["schema"] = "estamp_test"
    settings.raw["dry_run"] = False
    settings.raw["orders"]["settle_minutes"] = 0
    settings.raw["intake"]["settle_minutes"] = 0
    settings.raw["intake"]["split_provider"] = "local"
    g = FakeGoogle()
    root = g.folder("Shriram eStamp (Python)")
    for key in ["upload_inbox", "upload_archive", "needs_review", "inventory_root", "orders_incoming",
                "orders_processed", "orders_rejected", "output_root", "backup_root"]:
        settings.raw["folders"][key] = g.folder(key, root)
    settings.raw["control_sheet_id"] = g.add("control", GSHEET, root, tabs={})
    db = DB(DB_URL, "estamp_test", max_size=12)
    db.init_schema()
    yield settings, db, g
    db.close()


def _ctx(settings, db, g):
    return Ctx(settings, db, g, ocr=FakeOcr())


def _vendor_folder(g, s, name="Paper Ease"):
    return g.folder_path(s.folder("upload_inbox"), [name])


def _stock(g, s, state, amount, n, start=1):
    pages = [samples.paper_page(samples.cert(state, start + i), amount) for i in range(n)]
    return g.add(f"vendor_{state}_{amount}_{start}.pdf", PDF, _vendor_folder(g, s), samples.merged(pages))


def _add_orders(g, s, lans):
    rows = [samples.H] + [[str(i + 1), lan, "c", "b", "MH", "200"] for i, lan in enumerate(lans)]
    return g.add("TEST_Shriram_MH_LANs_26Oct", GSHEET, s.folder("orders_incoming"), tabs={"x": rows})


def _mh200_sheet(g):
    """The single MH-200 output tab's rows, or None if the sheet was never created."""
    for sid, tabs in g.sheets_data.items():
        if sid in g.files and g.files[sid]["name"] == "MH-200":
            return next(iter(tabs.values()))
    return None


def _inventory_pdfs(g):
    return [f for f in g.files.values() if f["mimeType"] == PDF and "inventory_root/" in g.path_of(f["id"])]


def _boom(msg):
    def f(*a, **kw):
        raise RuntimeError(msg)
    return f


def _age_allocating(db):
    """Make the stuck order/paper old enough that the next cycle's recovery sweeps them to ERROR."""
    db.q("UPDATE orders SET updated_at = now() - interval '30 minutes' WHERE status='ALLOCATING'")
    db.q("UPDATE papers SET updated_at = now() - interval '30 minutes' WHERE status='RESERVED'")


def _assert_issued_once(db, g, lan):
    """The LAN is DONE, issued exactly once, with its file really sitting as <LAN>.pdf in Output; no paper reused."""
    allocs = db.q("SELECT * FROM allocations WHERE lan=%s AND cancelled_at IS NULL", (lan,))
    assert len(allocs) == 1, f"{lan} issued {len(allocs)} times"
    assert db.q1("SELECT status FROM orders WHERE lan=%s", (lan,))["status"] == "DONE"
    assert g.path_of(allocs[0]["pdf_file_id"]).endswith(f"/{lan}.pdf")
    live = db.q1("SELECT count(*) a, count(DISTINCT paper_id) p FROM allocations WHERE cancelled_at IS NULL")
    assert live["a"] == live["p"], "a paper was issued twice"


# ---------------------------------------------------------------- intake stocking

@e2e
def test_crash_during_intake_stocking_stocks_the_paper_once(env):
    """Crash after the paper's PDF is uploaded to Inventory but before its ledger row commits. The next cycle must
    reuse the orphan file (not create a second) and stock the paper exactly once."""
    s, db, g = env
    _stock(g, s, "MH", 200, 1)
    orig = db.insert_paper
    db.insert_paper = _boom("crash after upload, before ledger insert")
    run_cycle(_ctx(s, db, g))                       # intake crashes mid-stock
    db.insert_paper = orig

    assert db.q1("SELECT count(*) n FROM papers")["n"] == 0, "crash left nothing in the ledger"
    assert len(_inventory_pdfs(g)) == 1, "the orphan file is already in Inventory"

    assert run_cycle(_ctx(s, db, g))                # fresh cycle resumes
    papers = db.q("SELECT * FROM papers")
    assert len(papers) == 1 and papers[0]["status"] == "AVAILABLE", "stocked exactly once"
    inv = _inventory_pdfs(g)
    assert len(inv) == 1, "orphan reused, not duplicated"
    assert papers[0]["drive_file_id"] == inv[0]["id"]


# ---------------------------------------------------------------- allocation crash points
# Each leaves the LAN/paper ALLOCATING/RESERVED. Recovery (once old enough) sweeps them to ERROR and the paper
# never issues. Invariant: at most one live allocation, no double-issue, no issued output row.

@e2e
@pytest.mark.parametrize("step,patch", [
    ("before backup", lambda g, db: setattr(g, "copy", _boom("crash before backup"))),
    ("after backup", lambda g, db: setattr(g, "move_rename", _boom("crash after backup, before move"))),
    ("after verify", lambda g, db: setattr(db, "finish_allocation", _boom("crash after verify, before ledger"))),
])
def test_crash_mid_allocation_recovers_to_error_without_double_issue(env, step, patch):
    s, db, g = env
    _stock(g, s, "MH", 200, 2)
    _add_orders(g, s, ["LAN-CRASH"])
    # First cycle: stock, then crash at the target allocation step.
    patch(g, db)
    run_cycle(_ctx(s, db, g))

    # The LAN never finished: nothing issued, no paper ASSIGNED, no issued output row.
    assert db.q1("SELECT count(*) n FROM allocations WHERE cancelled_at IS NULL")["n"] == 0, step
    assert db.q1("SELECT count(*) n FROM papers WHERE status='ASSIGNED'")["n"] == 0, step
    sheet = _mh200_sheet(g)
    assert sheet is None or len(sheet) == 1, f"{step}: no issued row written: {sheet}"

    # Recovery only ERRORs a genuinely stuck one (old enough) – age it, then run a clean fresh cycle.
    _age_allocating(db)
    assert run_cycle(_ctx(s, db, g))

    # Still never issued. The LAN ended ERROR (cleanly recoverable); a human RETRIES.
    assert db.q1("SELECT count(*) n FROM allocations WHERE cancelled_at IS NULL")["n"] == 0, step
    assert db.q1("SELECT status FROM orders WHERE lan='LAN-CRASH'")["status"] == "ERROR", step
    sheet = _mh200_sheet(g)
    assert sheet is None or len(sheet) == 1, f"{step}: output sheet must have no issued rows: {sheet}"


@e2e
def test_crash_after_ledger_commit_before_outputs_issues_once_on_resume(env):
    """The allocation ledger transaction committed (paper ASSIGNED, order DONE, allocation row written) but the
    process died before any output sheet existed – run_outputs never ran. A fresh run_outputs must write the sheet
    row exactly once and never re-issue the paper. This brackets the 'after verify' crash from the committed side."""
    from estamp.outputs import run_outputs
    s, db, g = env
    _stock(g, s, "MH", 200, 1)
    _add_orders(g, s, ["LAN-COMMITTED"])

    # Crash the cycle's output step so the ledger is committed but the sheet is never written – exactly the state a
    # real crash between finish_allocation and run_outputs would leave.
    orig = db.unwritten_allocations
    db.unwritten_allocations = _boom("crash before outputs")
    run_cycle(_ctx(s, db, g))
    db.unwritten_allocations = orig

    assert db.q1("SELECT count(*) n FROM allocations")["n"] == 1, "ledger committed before the crash"
    assert db.q1("SELECT count(*) n FROM allocations WHERE NOT output_written")["n"] == 1, "crash left it unwritten"
    assert _mh200_sheet(g) is None, "no output sheet was written before the crash"

    run_outputs(_ctx(s, db, g))                     # resume: write the pending sheet row once
    _assert_issued_once(db, g, "LAN-COMMITTED")
    sheet = _mh200_sheet(g)
    assert sheet is not None and len(sheet) == 2, f"one header + one issued row: {sheet}"
    assert [r[0] for r in sheet[1:]] == ["LAN-COMMITTED"]
    assert db.q1("SELECT count(*) n FROM allocations WHERE NOT output_written")["n"] == 0


@e2e
def test_crash_during_output_sheet_write_never_duplicates_the_row(env):
    """Crash after append_rows lands the row in the sheet but before mark_written flags the ledger (E-01). The
    next cycle re-fetches the still-unwritten allocation and must NOT append the LAN a second time."""
    s, db, g = env
    _stock(g, s, "MH", 200, 1)
    _add_orders(g, s, ["LAN-OUT"])

    orig = db.mark_written
    db.mark_written = _boom("crash after append_rows, before mark_written")
    run_cycle(_ctx(s, db, g))
    db.mark_written = orig

    sheet = _mh200_sheet(g)
    assert sheet is not None and len(sheet) == 2, "row landed in the sheet before the crash"
    assert db.q1("SELECT count(*) n FROM allocations WHERE NOT output_written")["n"] == 1, "crash left it unwritten"

    assert run_cycle(_ctx(s, db, g))                # re-fetches the unwritten alloc – must dedupe by LAN
    sheet = _mh200_sheet(g)
    assert len(sheet) == 2, f"LAN appended twice after crash+rerun: {sheet}"
    assert [r[0] for r in sheet[1:]] == ["LAN-OUT"]
    assert db.q1("SELECT count(*) n FROM allocations WHERE NOT output_written")["n"] == 0
    _assert_issued_once(db, g, "LAN-OUT")


# TODO(E-09): "crash after the rename+move" as a point *distinct* from "after verify" has no seam to patch without a
# source hook. In _allocate_one, move_rename is immediately followed by verify_issued inside the same try-block;
# there is no function invoked strictly between the move succeeding and verify starting. The two parametrized cases
# "after backup" (crash at move_rename, before the file is renamed into Output) and "after verify" (crash in
# finish_allocation, after the file sits in Output and OCR-verified) bracket it, and both prove no double-issue, so
# the gap is covered in effect. A dedicated post-move crash would need a seam in estamp/allocate.py (out of scope).
