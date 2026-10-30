"""H-1: customer page text stays out of the Log tab / events / alerts, and stored page text is pruned after a
retention window ("keep fields, prune text"). recheck-review keeps working from the stored OCR API fields.

Setup mirrors tests/test_crash_resume.py (own fixture, no private imports from test_all).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from estamp.context import Ctx, redact  # noqa: E402
from estamp.google_clients import GSHEET, PDF  # noqa: E402
from estamp.ocr_split import OcrResult  # noqa: E402
from estamp.settings import load_settings  # noqa: E402

import samples  # noqa: E402
from fake_apis import FakeApiOcr, FakeOcr  # noqa: E402
from fake_google import FakeGoogle  # noqa: E402

DB_URL = os.environ.get("ESTAMP_TEST_DB_URL")
e2e = pytest.mark.skipif(not DB_URL, reason="set ESTAMP_TEST_DB_URL to run end-to-end tests")
SECRET = "RAMESH KUMAR 12 MG ROAD"   # stands in for customer text on a page


@pytest.fixture
def env():
    import psycopg
    from estamp.db import DB
    settings = load_settings(ROOT / "config.yaml")
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


def _inbox(g, s):
    return g.folder_path(s.folder("upload_inbox"), ["Paper Ease"])


def _age_papers(db, days):
    db.q("UPDATE papers SET added_at = now() - make_interval(days => %s), updated_at = now() - make_interval(days => %s)",
         (days, days))


# ---------------------------------------------------------------- no page text in events / alerts

def test_redact_strips_postgres_row_and_parameter_echoes():
    """psycopg errors carry the failing row / JSON fragment / bound parameters – i.e. page text – in DETAIL/CONTEXT."""
    not_null = ('null value in column "status" of relation "papers" violates not-null constraint\n'
                f"DETAIL:  Failing row contains (1, 2, {SECRET}, null).")
    json_err = ("unsupported Unicode escape sequence\nDETAIL:  \\u0000 cannot be converted to text.\n"
                f'CONTEXT:  JSON data, line 1: {{"text": "{SECRET} \\u0000...\n'
                f"unnamed portal parameter $1 = '{SECRET}'")
    for msg in (not_null, json_err, f"step failed: {json_err}\nTraceback …\npsycopg.errors.X: {json_err}"):
        out = redact(msg)
        assert SECRET not in out, out
        assert out.splitlines()[0] == msg.splitlines()[0]          # the useful first line survives
    dup = 'duplicate key value violates unique constraint\nDETAIL:  Key (cert_no)=(IN-MH1A) already exists.'
    assert redact(dup) == dup                                       # cert numbers are fine to show
    assert redact("plain message") == "plain message"


def test_ocr_api_error_never_echoes_the_response_body(monkeypatch):
    """A failed OCR call must not put the raw response (which can hold the page reading) into the error text that
    lands in events and alerts."""
    import estamp.http_api as http_api
    import estamp.ocr_split as ocr_split

    class Resp:
        status_code, headers = 500, {}
        content = json.dumps({"success": False, "data": {"text": SECRET}}).encode()

        def json(self):
            return json.loads(self.content)

    settings = load_settings(ROOT / "config.yaml")
    monkeypatch.setenv("ZOOP_OCR_API_KEY", "test-key-not-real")
    monkeypatch.setenv("ZOOP_OCR_ORG_ACC", "test-org-not-real")
    monkeypatch.setattr(http_api, "_sleep", lambda s: None)
    monkeypatch.setattr(http_api, "_limiters", {})
    monkeypatch.setattr(ocr_split.requests, "post", lambda url, **kw: Resp())
    with pytest.raises(RuntimeError, match="OCR API HTTP 500") as e:
        ocr_split._ocr_zoop(b"%PDF-page", "p1", settings)
    assert SECRET not in str(e.value)


@e2e
def test_db_error_with_page_text_stays_out_of_log_and_alerts(env):
    """Real path: a reading Postgres can't store (\\u0000 in the JSON) fails the insert; the psycopg error echoes the
    JSON (page text) – it must not reach the events table (Log tab) or the alert list."""
    from estamp.cycle import run_cycle
    s, db, g = env

    class NulOcr(FakeApiOcr):
        def read(self, data, name):
            r = super().read(data, name)
            return OcrResult("", "", r.via, fields=r.fields, raw={"text": f"{SECRET} \u0000 tail"})

    g.add("nul.pdf", PDF, _inbox(g, s),
          samples.api_page(certification_no="IN-MH90000000000001A", state_code="MH", stamp_duty_amount="200"))
    ctx = Ctx(s, db, g, ocr=NulOcr())
    run_cycle(ctx)
    details = " ".join(e["details"] for e in db.q("SELECT details FROM events"))
    assert "nul.pdf" in details                                    # the failure is still reported …
    assert SECRET not in details and not any(SECRET in a for a in ctx.alerts)   # … without the page text


# ---------------------------------------------------------------- retention prune

def _stock_text_and_api(s, db, g):
    from estamp.cycle import run_cycle
    g.add("text.pdf", PDF, _inbox(g, s), samples.paper_page(samples.cert("MH", 1), 200))
    assert run_cycle(Ctx(s, db, g, ocr=FakeOcr()))
    g.add("api.pdf", PDF, _inbox(g, s),
          samples.api_page(certification_no="IN-MH90000000000002A", state_code="MH", stamp_duty_amount="200"))
    assert run_cycle(Ctx(s, db, g, ocr=FakeApiOcr()))
    rows = db.q("SELECT id, ocr_text, ocr_raw FROM papers ORDER BY id")
    assert len(rows) == 2 and rows[0]["ocr_text"] and rows[1]["ocr_raw"]["fields"]
    db.q("UPDATE papers SET ocr_raw = ocr_raw || '{\"responses\": {\"1\": {\"text\": \"x\"}}}' WHERE id=%s",
         (rows[1]["id"],))
    return rows


@e2e
def test_prune_clears_old_page_text_keeps_fields_and_is_idempotent(env):
    s, db, g = env
    text_row, api_row = _stock_text_and_api(s, db, g)
    assert db.prune_retention(30, 90) == (0, 0)                      # nothing old yet
    _age_papers(db, 31)
    before = {r["id"]: r["updated_at"] for r in db.q("SELECT id, updated_at FROM papers")}
    assert db.prune_retention(30, 90)[0] == 2
    rows = {r["id"]: r for r in db.q("SELECT * FROM papers")}
    assert rows[text_row["id"]]["ocr_text"] is None and rows[text_row["id"]]["ocr_raw"] is None
    assert rows[api_row["id"]]["ocr_raw"] == {"fields": api_row["ocr_raw"]["fields"]}   # fields kept, rest gone
    assert {i: r["updated_at"] for i, r in rows.items()} == before   # the prune is not a business change
    assert db.prune_retention(30, 90) == (0, 0)                      # idempotent


@e2e
def test_prune_deletes_only_old_events(env):
    s, db, g = env
    db.event("INFO", "Old", "old one")
    db.event("INFO", "New", "new one")
    db.q("UPDATE events SET ts = now() - interval '91 days' WHERE action='Old'")
    assert db.prune_retention(30, 90) == (0, 1)
    assert [e["action"] for e in db.q("SELECT action FROM events")] == ["New"]


@e2e
def test_prune_window_zero_disables_it(env):
    s, db, g = env
    _stock_text_and_api(s, db, g)
    _age_papers(db, 400)
    db.q("UPDATE events SET ts = now() - interval '400 days'")
    assert db.prune_retention(0, 0) == (0, 0)


@e2e
def test_cycle_runs_the_prune_from_config(env, monkeypatch):
    from estamp.cycle import run_cycle
    s, db, g = env
    _stock_text_and_api(s, db, g)
    monkeypatch.setitem(s.raw, "retention", {"ocr_text_days": 5, "events_days": 10})
    _age_papers(db, 6)
    db.q("DELETE FROM alerts_sent WHERE alert_key='retention'")      # the stocking cycles already ran today's prune
    assert run_cycle(Ctx(s, db, g, ocr=FakeOcr()))
    assert db.q1("SELECT count(*) n FROM papers WHERE ocr_text IS NOT NULL")["n"] == 0
    assert all(set(r["ocr_raw"] or {}) <= {"fields"} for r in db.q("SELECT ocr_raw FROM papers"))


@e2e
def test_later_edits_do_not_keep_text_alive(env):
    """Aged from stocking (added_at): a paper touched yesterday (recheck, ops edit) is still pruned."""
    s, db, g = env
    _stock_text_and_api(s, db, g)
    _age_papers(db, 31)
    db.q("UPDATE papers SET updated_at = now() - interval '1 day'")
    assert db.prune_retention(30, 90)[0] == 2


@e2e
def test_cycle_prunes_once_a_day(env, monkeypatch):
    from estamp.cycle import run_cycle
    s, db, g = env
    _stock_text_and_api(s, db, g)                                   # these two cycles already claimed today
    _age_papers(db, 31)
    kept = db.q1("SELECT count(*) n FROM papers WHERE ocr_text IS NOT NULL")["n"]
    assert kept > 0
    assert run_cycle(Ctx(s, db, g, ocr=FakeOcr()))
    assert db.q1("SELECT count(*) n FROM papers WHERE ocr_text IS NOT NULL")["n"] == kept   # not again today
    db.q("DELETE FROM alerts_sent WHERE alert_key='retention'")      # next day
    assert run_cycle(Ctx(s, db, g, ocr=FakeOcr()))
    assert db.q1("SELECT count(*) n FROM papers WHERE ocr_text IS NOT NULL")["n"] == 0


@e2e
def test_blank_retention_config_uses_the_default(env, monkeypatch):
    from estamp.cycle import _prune
    s, db, g = env
    _stock_text_and_api(s, db, g)
    db.q("DELETE FROM alerts_sent WHERE alert_key='retention'")
    monkeypatch.setitem(s.raw, "retention", {"ocr_text_days": None, "events_days": None})
    _age_papers(db, 31)
    _prune(Ctx(s, db, g, ocr=FakeOcr()))
    assert db.q1("SELECT count(*) n FROM papers WHERE ocr_text IS NOT NULL")["n"] == 0


def test_shipped_config_retention_defaults():
    s = load_settings(ROOT / "config.yaml")
    assert int(s.get("retention.ocr_text_days")) == 30 and int(s.get("retention.events_days")) == 90


# ---------------------------------------------------------------- recheck-review after the prune

@e2e
def test_recheck_review_works_from_fields_after_text_was_pruned(env, monkeypatch):
    from estamp.actions import recheck_review
    from estamp.cycle import run_cycle
    s, db, g = env
    monkeypatch.setattr(s.states["HR"], "amounts", [100])   # ₹101 must start out not allowed
    g.add("hr101.pdf", PDF, _inbox(g, s),
          samples.api_page(certification_no="G1K2026A991", state_code="HR", stamp_duty_amount="101"))
    assert run_cycle(Ctx(s, db, g, ocr=FakeApiOcr()))
    assert db.q1("SELECT status FROM papers")["status"] == "NEEDS_REVIEW"
    _age_papers(db, 31)
    assert db.prune_retention(30, 90)[0] == 1
    p = db.q1("SELECT * FROM papers")
    assert p["ocr_text"] is None and set(p["ocr_raw"]) == {"fields"}
    monkeypatch.setattr(s.states["HR"], "amounts", [100, 101])
    ocr = FakeApiOcr()
    assert recheck_review(Ctx(s, db, g, ocr=ocr)) == (1, 0) and ocr.calls == []
    p = db.q1("SELECT status, cert_no, state, denomination FROM papers")
    assert (p["status"], p["state"], p["denomination"]) == ("AVAILABLE", "HR", 101) and "G1K2026A991" in p["cert_no"]


@e2e
def test_recheck_review_with_neither_text_nor_fields_stays_in_review(env):
    from estamp.actions import recheck_review
    from estamp.cycle import run_cycle
    s, db, g = env
    g.add("mh999.pdf", PDF, _inbox(g, s), samples.paper_page(samples.cert("MH", 7), 999))
    assert run_cycle(Ctx(s, db, g, ocr=FakeOcr()))
    p = db.q1("SELECT * FROM papers")
    assert p["status"] == "NEEDS_REVIEW" and p["ocr_text"] and p["ocr_raw"] is None
    _age_papers(db, 31)
    db.prune_retention(30, 90)
    assert recheck_review(Ctx(s, db, g, ocr=FakeOcr())) == (0, 1)
    assert db.q1("SELECT status, notes FROM papers") == {"status": "NEEDS_REVIEW", "notes": p["notes"]}
