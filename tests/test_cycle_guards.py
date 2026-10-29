"""Cycle-level guards around the single-writer lock (docs/AUDIT.md):

  M-2  the advisory lock lives on its own connection – if it drops mid-run, the cycle must stop, not carry on
       alongside a second process that could now take the lock.
  M-7  a `runs` row left open by a hard crash must not make the next lock-holder look "wedged".
  L-3  alerts go out ERROR-first, so the 40-line cap never drops a critical.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from estamp import cycle  # noqa: E402
from estamp.context import Ctx, Stats  # noqa: E402
from estamp.cycle import run_cycle  # noqa: E402
from estamp.google_clients import GSHEET  # noqa: E402
from estamp.settings import Settings, load_settings  # noqa: E402

from fake_apis import FakeOcr  # noqa: E402
from fake_google import FakeGoogle  # noqa: E402

DB_URL = os.environ.get("ESTAMP_TEST_DB_URL")
e2e = pytest.mark.skipif(not DB_URL, reason="set ESTAMP_TEST_DB_URL to run end-to-end tests")


@pytest.fixture
def env():
    import psycopg
    from estamp.db import DB
    settings = load_settings(ROOT / "config.yaml")
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS estamp_test CASCADE")
    settings.raw["db"]["schema"] = "estamp_test"
    settings.raw["dry_run"] = False
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


# ------------------------------------------------------------ M-2: lock lost mid-run

@e2e
def test_lock_held_tracks_the_lock_session(env):
    s, db, g = env
    assert not db.lock_held()                                    # never taken
    assert db.try_lock() and db.lock_held()
    db._lock_conn.execute("SELECT pg_advisory_unlock_all()")    # session alive, lock gone
    assert not db.lock_held()
    db.release_lock()
    assert db.try_lock()
    db._lock_conn.close()                                        # session dropped → Postgres released the lock
    assert not db.lock_held()


@e2e
def test_losing_the_lock_mid_cycle_stops_every_later_step(env, monkeypatch):
    s, db, g = env
    ran = []

    def ingest_then_drop_lock(*a, **kw):
        ran.append("Ingest")
        db._lock_conn.close()        # network blip / Postgres idle killer: the lock session is gone

    monkeypatch.setattr(cycle, "run_ingest", ingest_then_drop_lock)
    for name in ("run_allocation", "run_intake", "run_outputs", "low_stock_alerts", "run_dashboard"):
        monkeypatch.setattr(cycle, name, lambda *a, _n=name, **kw: ran.append(_n))

    ctx = _ctx(s, db, g)
    assert run_cycle(ctx) is False
    assert ran == ["Ingest"], f"steps ran after the lock was lost: {ran}"
    errors = [e["details"] for e in db.q("SELECT details FROM events WHERE level='ERROR' AND action='Cycle'")]
    assert any("lock" in d for d in errors), errors
    assert any("lock" in a for a in ctx.alerts if a.startswith("ERROR"))
    assert db.q1("SELECT count(*) n FROM runs WHERE finished_at IS NULL")["n"] == 0, "the run is still closed out"


@e2e
def test_a_healthy_cycle_still_runs_every_step(env, monkeypatch):
    s, db, g = env
    ran = []
    for name in ("run_actions", "run_ingest", "run_allocation", "run_intake", "run_outputs", "low_stock_alerts",
                 "run_dashboard"):
        monkeypatch.setattr(cycle, name, lambda *a, _n=name, **kw: ran.append(_n))
    assert run_cycle(_ctx(s, db, g)) is True
    assert ran == ["run_actions", "run_ingest", "run_allocation", "run_intake", "run_allocation", "run_outputs",
                   "low_stock_alerts", "run_dashboard"]


# ------------------------------------------------------------ M-7: stale crashed run vs. wedged alarm

@e2e
def test_a_stale_crashed_run_does_not_raise_a_false_wedged_alarm(env):
    from estamp.db import DB
    s, db, g = env
    # A process that died hard (power loss) 3 h ago: its runs row was never finished.
    db.q("INSERT INTO runs(started_at) VALUES (now() - interval '3 hours')")
    seen = {}

    def third_cycle_tries_meanwhile(ok):
        # The healthy holder has finished its run row but still holds the lock while it wraps up (alerts,
        # heartbeat). Another cycle can't get the lock and checks how long "the" running cycle has been going.
        other = DB(DB_URL, "estamp_test", max_size=2)
        try:
            ctx2 = _ctx(s, other, g)
            assert run_cycle(ctx2)
            seen["alerts"] = list(ctx2.alerts)
        finally:
            other.close()

    ctx = _ctx(s, db, g)
    ctx.heartbeat = third_cycle_tries_meanwhile
    assert run_cycle(ctx)
    assert seen["alerts"] == [], f"false wedged alarm: {seen['alerts']}"
    assert db.q1("SELECT count(*) n FROM events WHERE details LIKE '%%wedged%%'")["n"] == 0
    assert db.q1("SELECT count(*) n FROM runs WHERE finished_at IS NULL")["n"] == 0
    crashed = db.q1("SELECT ok, summary FROM runs ORDER BY id LIMIT 1")
    assert crashed["ok"] is False and crashed["summary"].get("crashed") is True


# ------------------------------------------------------------ L-3: ERROR-first alerts

def test_alerts_go_out_error_first_so_the_cap_never_drops_a_critical():
    ctx = Ctx.__new__(Ctx)                                     # no DB/Google: _send only formats + hands off
    ctx.settings = Settings(raw={"dry_run": False, "alerts": {"daily_summary_hour": 99}}, base_dir=Path("."))
    ctx.stats, ctx.dry = Stats(), False
    ctx.alerts = [f"WARN · Intake: w{i}" for i in range(45)] + ["ERROR · Allocate: critical A",
                                                                 "WARN · Outputs: late", "ERROR · Cycle: critical B"]
    sent = []
    ctx.send_alerts = lambda subject, lines: sent.append(lines)
    cycle._send(ctx)
    bullets = [ln for ln in sent[0] if ln.startswith("• ")]
    assert bullets[:2] == ["• ERROR · Allocate: critical A", "• ERROR · Cycle: critical B"]
    assert bullets[2:] == [f"• WARN · Intake: w{i}" for i in range(38)]          # stable within a level
    assert sent[0][-1] == "… 8 more – see the Log tab"


def test_lock_connection_has_tcp_keepalives(monkeypatch):
    # A half-open lock session must error out (→ lock_held False) rather than hang the cycle forever.
    import psycopg

    from estamp.db import DB
    seen = {}

    class Conn:
        def execute(self, *a):
            return self

        def fetchone(self):
            return (True,)

    def fake_connect(url, **kw):
        seen.update(kw)
        return Conn()

    db = DB.__new__(DB)
    db.url, db.schema, db._lock_conn = "postgresql://x", "estamp_test", None
    monkeypatch.setattr(psycopg, "connect", fake_connect)
    assert db.try_lock()
    assert seen.get("keepalives") == 1 and seen.get("connect_timeout")
