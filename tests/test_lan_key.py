"""M-6 / ADR-0008: one canonical LAN key everywhere a LAN is matched (all whitespace removed + uppercase;
leading zeros stay significant). The raw LAN is kept for display. A spaced and an unspaced copy of the same loan
must never become two orders, and never two certificates."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from estamp.extract import StateResolver, canonical_lan, parse_order_tabs  # noqa: E402
from estamp.google_clients import GSHEET, PDF  # noqa: E402
from estamp.settings import load_settings  # noqa: E402

import samples  # noqa: E402
from fake_google import FakeGoogle  # noqa: E402

LAN = "TGHYDTW2610260001"          # Shriram's real shape: 7 capitals + 10 digits
SPACED = "TGHYDTW 2610260001"


@pytest.fixture(scope="session")
def settings():
    return load_settings(ROOT / "config.yaml")


# ============================================================ pure logic

@pytest.mark.parametrize("raw", [LAN, SPACED, " tghydtw 2610260001 ", "TGHYDTW\t2610260001",
                                 "TGHYDTW 2610260001", "TGH YDT W 26 1026 0001", "tghydtw2610260001",
                                 "TGHYDTW 2610260001\n"])
def test_canonical_lan_removes_all_whitespace_and_uppercases(raw):
    assert canonical_lan(raw) == LAN


def test_canonical_lan_uppercases_ascii_only():
    # 'ﬁ'.upper() == 'FI' and 'ß'.upper() == 'SS': text pasted with those must never match a real ASCII LAN,
    # whether it comes from an order sheet or is typed by ops into Order Actions / Manual Issues.
    assert canonical_lan("tghydtw261026000ﬁ") == "TGHYDTW261026000ﬁ" != canonical_lan("TGHYDTW261026000FI")
    assert canonical_lan("abß") == "ABß"


def test_display_lan_has_whitespace_runs_collapsed_to_one_space(settings):
    # The raw LAN names <LAN>.pdf and fills the Output sheet: no tabs, NBSPs or runs of spaces may reach them.
    res = _parse(settings, [["1", "TGHYDTW\t  2610260001", "c", "b", "MH", "200"],
                            ["2", "TGHYDTW" + " " * 500 + "2610260002", "c", "b", "MH", "200"]])
    assert [o.lan for o in res.valid] == [SPACED, "TGHYDTW 2610260002"], res


def test_canonical_lan_keeps_leading_zeros_significant():
    assert canonical_lan("0012345") == "0012345"
    assert canonical_lan("0012345") != canonical_lan("12345")


def _parse(settings, rows, name="TEST_Shriram_MH_LANs"):
    return parse_order_tabs([("x", [samples.H] + rows)], name, settings, StateResolver(settings.states))


def test_spaced_and_unspaced_lan_in_one_file_are_duplicates(settings):
    res = _parse(settings, [["1", SPACED, "c", "b", "MH", "200"], ["2", LAN, "c", "b", "MH", "200"]])
    assert res.invalid == [], "an internal space must not fail the LAN format check"
    assert len(res.valid) == 1 and len(res.duplicates) == 1, res
    assert res.valid[0].lan == SPACED, "raw LAN kept for display"
    assert res.valid[0].lan_key == LAN


def test_lowercase_lan_in_one_file_is_a_duplicate(settings):
    res = _parse(settings, [["1", LAN.lower(), "c", "b", "MH", "200"], ["2", LAN, "c", "b", "MH", "200"]])
    assert len(res.valid) == 1 and len(res.duplicates) == 1, res


def test_lan_format_check_still_rejects_bad_characters(settings):
    res = _parse(settings, [["1", "TGHYDTW#2610260001", "c", "b", "MH", "200"],
                            ["2", "TGHYDTW2610260ﬁ", "c", "b", "MH", "200"]])   # 'ﬁ'.upper() == 'FI'
    assert res.valid == [] and len(res.invalid) == 2 and all("fails format check" in m for m in res.invalid)


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


def _ctx(s, db, g):
    from estamp.context import Ctx
    from fake_apis import FakeOcr
    return Ctx(s, db, g, ocr=FakeOcr())


def _stock(g, s, state, amount, n, start=1):
    pages = [samples.paper_page(samples.cert(state, start + i), amount) for i in range(n)]
    g.add(f"vendor_{state}_{amount}_{start}.pdf", PDF, g.folder_path(s.folder("upload_inbox"), ["Paper Ease"]),
          samples.merged(pages))


def _order_file(g, s, name, rows, age):
    g.add(name, GSHEET, s.folder("orders_incoming"), tabs={"x": [samples.H] + rows}, age_min=age)


@e2e
def test_spaced_lan_in_two_files_for_two_states_is_conflict_never_two_allocations(env):
    from estamp.cycle import run_cycle
    s, db, g = env
    _order_file(g, s, "TEST_Shriram_MH_LANs", [["1", SPACED, "c", "b", "MH", "200"]], 20)
    _order_file(g, s, "TEST_Shriram_KA_LANs", [["1", LAN.lower(), "c", "b", "KARNATAKA", "500"]], 10)
    _stock(g, s, "MH", 200, 2)
    _stock(g, s, "KA", 500, 2, start=10)
    assert run_cycle(_ctx(s, db, g))
    rows = db.q("SELECT lan, lan_key, state, status FROM orders ORDER BY id")
    assert [(r["state"], r["status"]) for r in rows] == [("MH", "CONFLICT"), ("KA", "CONFLICT")], rows
    assert {r["lan_key"] for r in rows} == {LAN}
    assert [r["lan"] for r in rows] == [SPACED, LAN.lower()], "raw LAN kept for display"
    assert db.q1("SELECT count(*) n FROM allocations")["n"] == 0


@e2e
def test_spaced_lan_in_two_files_for_one_state_is_issued_once(env):
    from estamp.cycle import run_cycle
    s, db, g = env
    _order_file(g, s, "TEST_Shriram_MH_LANs_a", [["1", LAN, "c", "b", "MH", "200"]], 20)
    _order_file(g, s, "TEST_Shriram_MH_LANs_b", [["1", SPACED, "c", "b", "MH", "200"]], 10)
    _stock(g, s, "MH", 200, 2)
    ctx = _ctx(s, db, g)
    assert run_cycle(ctx)
    assert db.q1("SELECT count(*) n FROM orders")["n"] == 1
    assert db.q1("SELECT count(*) n FROM allocations")["n"] == 1
    assert ctx.stats.duplicates == 1


def _order_action(s, db, g, row):
    from estamp.actions import ORDER_HEADER, ORDER_TAB, run_actions
    sid = s.get("control_sheet_id")
    g.ensure_tabs(sid, {ORDER_TAB: ORDER_HEADER})
    g.sheets_data[sid][ORDER_TAB].append(row)
    run_actions(_ctx(s, db, g))
    return g.sheets_data[sid][ORDER_TAB][-1][3]


@e2e
def test_order_actions_find_the_lan_whatever_spacing_or_case_ops_type(env):
    s, db, g = env
    db.insert_orders([(LAN, LAN, "MH", 200, "f", "fid", "t", 1, "ERROR", "boom"),
                      ("TGHYDTW2610260002", "TGHYDTW2610260002", "MH", 200, "f", "fid", "t", 2, "PENDING", None),
                      ("TGHYDTW2610260003", "TGHYDTW2610260003", "MH", 200, "f", "fid", "t", 3, "CONFLICT", None),
                      ("TGHYDTW2610260003", "TGHYDTW2610260003", "KA", 500, "f", "fid", "t", 4, "CONFLICT", None)])
    assert _order_action(s, db, g, [" tghydtw 2610260001", "RETRY", ""]) == "DONE: back to PENDING"
    assert _order_action(s, db, g, ["TGHYDTW 2610260002", "CANCEL", ""]).startswith("DONE: cancelled")
    assert _order_action(s, db, g, ["tghydtw 26102 60003", "KEEP_STATE", "MH"]) == "DONE: MH kept, others cancelled"
    st = {(r["lan_key"], r["state"]): r["status"] for r in db.q("SELECT lan_key, state, status FROM orders")}
    assert st == {(LAN, "MH"): "PENDING", ("TGHYDTW2610260002", "MH"): "CANCELLED",
                  ("TGHYDTW2610260003", "MH"): "PENDING", ("TGHYDTW2610260003", "KA"): "CANCELLED"}, st


@e2e
def test_manual_issue_finds_a_spaced_lan(env):
    from estamp.actions import MANUAL_HEADER, MANUAL_TAB, run_actions
    s, db, g = env
    cert = samples.cert("MH", 900)
    db.insert_orders([(LAN, LAN, "MH", 200, "f", "fid", "t", 1, "PENDING", None)])
    db.insert_paper(state="MH", denomination=200, status="AVAILABLE", cert_no=cert)
    sid = s.get("control_sheet_id")
    g.ensure_tabs(sid, {MANUAL_TAB: MANUAL_HEADER})
    g.sheets_data[sid][MANUAL_TAB].append([SPACED.lower(), cert, "ravi", "", ""])
    run_actions(_ctx(s, db, g))
    assert g.sheets_data[sid][MANUAL_TAB][-1][3].startswith("DONE: recorded manual issue"), g.sheets_data[sid]
    assert db.q1("SELECT status FROM orders")["status"] == "DONE"


@e2e
def test_output_sheet_dedup_matches_the_lan_in_canonical_form(env):
    """E-01 guard, keyed on the canonical LAN: if the row already in the sheet spells the LAN differently
    (ops tidied the spacing / case), a re-run after a crash still must not append it a second time."""
    from estamp.cycle import run_cycle
    from estamp.outputs import run_outputs
    s, db, g = env
    _stock(g, s, "MH", 200, 1)
    _order_file(g, s, "TEST_Shriram_MH_LANs", [["1", SPACED, "c", "b", "MH", "200"]], 10)
    orig = db.mark_written
    db.mark_written = lambda ids: (_ for _ in ()).throw(RuntimeError("crash after append"))
    run_cycle(_ctx(s, db, g))
    db.mark_written = orig
    sid = next(i for i, f in g.files.items() if f["name"] == "MH-200")
    rows = next(iter(g.sheets_data[sid].values()))
    assert len(rows) == 2 and rows[1][0] == SPACED
    rows[1][0] = LAN.lower()
    run_outputs(_ctx(s, db, g))
    assert len(next(iter(g.sheets_data[sid].values()))) == 2, "LAN appended twice"


# ------------------------------------------------------------ migration of ledger rows keyed the old way

def _legacy(db, lan, state, status, key=None):
    """An order row as the pre-ADR-0008 code stored it: lan_key = strip + upper only."""
    return db.q1("INSERT INTO orders(lan, lan_key, state, denomination, status) VALUES (%s,%s,%s,200,%s) RETURNING id",
                 (lan, key if key is not None else lan.strip().upper(), state, status))["id"]


@e2e
def test_out_of_date_lan_keys_are_reported_and_init_db_rekeys_them(env):
    s, db, g = env
    a = _legacy(db, SPACED, "MH", "PENDING")
    b = _legacy(db, "TGHYDTW 2610260009", "MH", "CANCELLED")
    c = _legacy(db, "TGHYDTW2610260005", "MH", "DONE")
    d = _legacy(db, "TGHYDTW2610260008", "MH", "ERROR", key="tghydtw2610260008")   # hand-inserted lowercase key
    e = _legacy(db, "TGHYDTW 2610260004", "MH", "DONE", key="TGHYDTW2610260004")   # already canonical
    f = _legacy(db, "TGHYDTW261026001ı", "MH", "CANCELLED")    # old .upper(): dotless 'ı' → ASCII 'I'
    assert db.missing_schema() == ["orders.lan_key not canonical (4 rows) – ADR-0008"]
    candidates = {r["id"] for r in db.q(db._LAN_REKEY_CANDIDATES)}
    assert {a, b, d, e, f} <= candidates and c not in candidates, "plain canonical rows are never even read"
    db.init_schema()
    keys = {r["id"]: (r["lan"], r["lan_key"]) for r in db.q("SELECT id, lan, lan_key FROM orders")}
    assert keys == {a: (SPACED, LAN), b: ("TGHYDTW 2610260009", "TGHYDTW2610260009"),
                    c: ("TGHYDTW2610260005", "TGHYDTW2610260005"), d: ("TGHYDTW2610260008", "TGHYDTW2610260008"),
                    e: ("TGHYDTW 2610260004", "TGHYDTW2610260004"),
                    f: ("TGHYDTW261026001ı", "TGHYDTW261026001ı")}
    assert db.missing_schema() == []
    db.init_schema()                                      # idempotent
    assert {r["id"]: (r["lan"], r["lan_key"]) for r in db.q("SELECT id, lan, lan_key FROM orders")} == keys


@e2e
@pytest.mark.parametrize("second_state, second_status", [("MH", "DONE"), ("KA", "PENDING"), ("KA", "CONFLICT")])
def test_two_open_rows_collapsing_to_one_key_fail_the_migration_and_change_nothing(env, second_state, second_status):
    from estamp.db import LanKeyCollision
    s, db, g = env
    _legacy(db, SPACED, "MH", "PENDING")
    _legacy(db, LAN, second_state, second_status)
    _legacy(db, "TGHYDTW 2610260009", "MH", "PENDING")       # an innocent row that would be re-keyed
    before = db.q("SELECT id, lan_key, status FROM orders ORDER BY id")
    with pytest.raises(LanKeyCollision, match=LAN) as exc:
        db.init_schema()
    assert "status='CANCELLED'" in str(exc.value), "tell the human exactly how to resolve it"
    assert db.q("SELECT id, lan_key, status FROM orders ORDER BY id") == before, "no auto-merge, no partial re-key"
    assert any("not canonical" in m for m in db.missing_schema()), "CLI keeps refusing until a human resolves it"


@e2e
def test_cancelled_history_does_not_block_the_migration(env):
    s, db, g = env
    _legacy(db, SPACED, "MH", "CANCELLED")
    _legacy(db, LAN, "MH", "DONE")
    db.init_schema()
    assert {r["lan_key"] for r in db.q("SELECT lan_key FROM orders")} == {LAN}
