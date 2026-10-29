"""Startup guards: an unfilled config placeholder and an out-of-date ledger schema fail loudly and clearly,
before any Drive call or query runs against a missing column."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from estamp.settings import load_settings

ROOT = Path(__file__).resolve().parents[1]
DB_URL = os.environ.get("ESTAMP_TEST_DB_URL")
e2e = pytest.mark.skipif(not DB_URL, reason="set ESTAMP_TEST_DB_URL to run end-to-end tests")


# ------------------------------------------------------------ #6: config placeholder

def _settings_with_folders(tmp_path, folders: dict):
    raw = {"state_config_file": str(ROOT / "state_config.csv"), "folders": folders}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_settings(p)


def test_placeholder_folder_id_counts_as_not_set(tmp_path):
    # The shipped config carries REPLACE_WITH_… until go-live; it must never reach Drive as a folder ID (404).
    s = _settings_with_folders(tmp_path, {"reconciliation_root": "REPLACE_WITH_RECONCILIATION_FOLDER_ID"})
    with pytest.raises(ValueError, match="folders.reconciliation_root is not set"):
        s.folder("reconciliation_root")


def test_real_folder_id_is_returned(tmp_path):
    s = _settings_with_folders(tmp_path, {"reconciliation_root": "1abcDEF_real-id"})
    assert s.folder("reconciliation_root") == "1abcDEF_real-id"


# ------------------------------------------------------------ #4: schema drift

def test_expected_schema_parses_tables_columns_and_indexes():
    from estamp.db import expected_schema
    cols, indexes = expected_schema()
    assert "vendor_code" in cols["papers"]                 # added by ALTER, not CREATE
    assert "cancelled_at" in cols["allocations"]
    assert cols["output_sheets"] == {"alloc_date", "state", "label", "spreadsheet_id"}   # no 'PRIMARY'
    assert "allocations_order_live_uq" in indexes          # the double-issue backstop


@pytest.fixture
def test_db():
    import psycopg
    from estamp.db import DB
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS estamp_test CASCADE")
    db = DB(DB_URL, "estamp_test", max_size=2)
    db.init_schema()
    yield db
    db.close()


@e2e
def test_fresh_schema_has_nothing_missing(test_db):
    assert test_db.missing_schema() == []


@e2e
def test_missing_column_and_index_are_reported(test_db):
    test_db.q("ALTER TABLE papers DROP COLUMN vendor_code")
    test_db.q("DROP INDEX allocations_order_live_uq")
    assert test_db.missing_schema() == ["papers.vendor_code", "index allocations_order_live_uq"]


def _cli_on_test_db(tmp_path, monkeypatch):
    """Point the CLI at estamp_test. Real config.yaml for parsing rules, but every live Drive/Sheet ID blanked,
    so nothing here can reach the real folders even if the guard regresses."""
    from estamp import cli
    from estamp.db import DB
    s = load_settings(ROOT / "config.yaml")
    s.raw["folders"] = {k: "" for k in s.raw["folders"]}
    s.raw["control_sheet_id"] = ""
    s.raw["db"]["schema"] = "estamp_test"
    monkeypatch.setattr(cli, "_build", lambda args, need_google=True, **kw: (s, DB(DB_URL, "estamp_test", max_size=2), None))
    return cli


@e2e
def test_cli_refuses_to_run_on_out_of_date_schema(test_db, tmp_path, monkeypatch):
    # The real failure: code pulled, init-db not run, `report` crashed mid-way with UndefinedColumn.
    # It must stop before doing anything, and say how to fix it.
    cli = _cli_on_test_db(tmp_path, monkeypatch)
    test_db.q("ALTER TABLE papers DROP COLUMN vendor_code")
    with pytest.raises(SystemExit) as exc:
        cli.main(["report", "--date", "2026-10-28"])
    msg = str(exc.value)
    assert "papers.vendor_code" in msg and "init-db" in msg


@e2e
def test_status_still_runs_on_out_of_date_schema_but_warns(test_db, tmp_path, monkeypatch, capsys):
    # `status` is read-only diagnostics – ops need it most exactly when something is off, so warn, don't refuse.
    cli = _cli_on_test_db(tmp_path, monkeypatch)
    test_db.q("DROP INDEX allocations_order_live_uq")
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "allocations_order_live_uq" in out and "init-db" in out
