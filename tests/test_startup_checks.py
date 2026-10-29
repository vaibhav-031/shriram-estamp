"""Startup guards for run / run-once and check-setup (docs/AUDIT.md), plus alert ordering:

  L-1  every required folder id / env key is checked once, before any Drive/DB work, and reported in one message.
  H-2  .env and the secrets files the service loads must not be group/other readable (POSIX; skipped on Windows).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

from estamp import cli
from estamp.settings import load_settings, secret_file_problems, startup_problems

ROOT = Path(__file__).resolve().parents[1]
FOLDERS = ["upload_inbox", "upload_archive", "needs_review", "inventory_root", "orders_incoming",
           "orders_processed", "orders_rejected", "output_root", "backup_root"]


def _config(tmp_path, monkeypatch, *, folders=None, dry_run=True, env=True, **extra) -> Path:
    """A self-contained config in tmp_path: its own env-var names (so a developer's real .env can't satisfy them)
    and its own Google token file (owner-only)."""
    token = tmp_path / "secrets" / "token.json"
    token.parent.mkdir()
    token.write_text("{}")
    token.chmod(0o600)
    raw = {
        "dry_run": dry_run,
        "state_config_file": str(ROOT / "state_config.csv"),
        "db": {"url_env": "T_DB_URL", "schema": "estamp_test"},
        "google": {"auth": "oauth", "oauth_client_file": "secrets/client_secret.json",
                   "oauth_token_file": "secrets/token.json"},
        "folders": {k: f"id_{k}" for k in FOLDERS} if folders is None else folders,
        "intake": {"split_provider": "zoop_api"},
        "zoop_api": {"split": {"url": "https://split.test"},
                     "ocr": {"url": "https://ocr.test", "config_id": "c1",
                             "api_key_env": "T_OCR_KEY", "org_acc_env": "T_OCR_ORG"}},
        "alerts": {"slack_webhook_env": "T_SLACK", "smtp": {"host": "", "to": []}},
        **extra,
    }
    for name in ("T_DB_URL", "T_OCR_KEY", "T_OCR_ORG", "T_SLACK"):
        monkeypatch.delenv(name, raising=False)
    if env:
        monkeypatch.setenv("T_DB_URL", "postgresql://nowhere.invalid/x")
        monkeypatch.setenv("T_OCR_KEY", "k")
        monkeypatch.setenv("T_OCR_ORG", "o")
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return p


# ------------------------------------------------------------ L-1: required config, up front

def test_complete_config_has_no_startup_problems(tmp_path, monkeypatch):
    assert startup_problems(load_settings(_config(tmp_path, monkeypatch))) == []


def test_every_missing_key_is_listed_at_once(tmp_path, monkeypatch):
    folders = {k: f"id_{k}" for k in FOLDERS}
    folders["output_root"] = ""
    folders["backup_root"] = "REPLACE_WITH_BACKUP_ID"
    del folders["needs_review"]
    s = load_settings(_config(tmp_path, monkeypatch, folders=folders, env=False))
    (tmp_path / "secrets" / "token.json").unlink()
    text = "\n".join(startup_problems(s))
    for want in ("folders.output_root", "folders.backup_root", "folders.needs_review",
                 "T_DB_URL", "T_OCR_KEY", "T_OCR_ORG", "oauth_token_file", "estamp login"):
        assert want in text, f"{want} missing from:\n{text}"
    assert "upload_inbox" not in text


def test_split_url_only_required_for_the_split_api(tmp_path, monkeypatch):
    s = load_settings(_config(tmp_path, monkeypatch))
    s.raw["zoop_api"]["split"]["url"] = ""
    assert any("zoop_api.split.url" in p for p in startup_problems(s))
    s.raw["intake"]["split_provider"] = "local"
    assert startup_problems(s) == []


def test_dry_run_config_needs_no_reconciliation_folder_or_alert_channel(tmp_path, monkeypatch):
    # config.dryrun.yaml deliberately has no reconciliation_root and its alert envs point at nothing.
    s = load_settings(ROOT / "config.dryrun.yaml")
    s.raw["dry_run"] = True
    token = tmp_path / "token.json"
    token.write_text("{}")
    s.raw["google"]["oauth_token_file"] = str(token)
    for k in ("db.url_env", "zoop_api.ocr.api_key_env", "zoop_api.ocr.org_acc_env"):
        monkeypatch.setenv(s.get(k), "set")
    assert "reconciliation_root" not in s.raw["folders"]
    assert startup_problems(s) == []
    monkeypatch.setattr(cli, "secret_file_problems", lambda s: [])   # the repo's own .env/secrets aren't under test
    cli._startup_checks(s)                                     # no alert channel is fine in dry run


def test_live_run_needs_an_alert_channel(tmp_path, monkeypatch):
    s = load_settings(_config(tmp_path, monkeypatch, dry_run=False))
    with pytest.raises(SystemExit, match="alert channel"):
        cli._startup_checks(s)
    monkeypatch.setenv("T_SLACK", "https://hooks.slack.test/x")
    cli._startup_checks(s)


def _no_db_or_google(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("DB/Google touched before the startup checks")
    monkeypatch.setattr(cli, "DB", boom)
    import estamp.google_clients as gc
    monkeypatch.setattr(gc, "Google", boom)


@pytest.mark.parametrize("command", ["run-once", "run"])
def test_run_refuses_before_any_drive_or_db_work(tmp_path, monkeypatch, command):
    _no_db_or_google(monkeypatch)
    cfg = _config(tmp_path, monkeypatch, folders={}, env=False)
    with pytest.raises(SystemExit) as exc:
        cli.main([command, "--config", str(cfg)])
    msg = str(exc.value)
    assert "folders.upload_inbox" in msg and "folders.backup_root" in msg and "T_DB_URL" in msg


# ------------------------------------------------------------ H-2: secret file permissions

def _with_env_file(tmp_path, monkeypatch, mode):
    cfg = _config(tmp_path, monkeypatch)
    env = tmp_path / ".env"
    env.write_text("T_UNUSED=1\n")
    env.chmod(mode)
    return load_settings(cfg), env.resolve()


def test_group_or_world_readable_secret_is_reported_with_the_exact_chmod(tmp_path, monkeypatch):
    s, env = _with_env_file(tmp_path, monkeypatch, 0o644)
    problems = secret_file_problems(s)
    assert len(problems) == 1, problems
    assert f"chmod 600 {env}" in problems[0]
    assert "T_UNUSED" not in problems[0]                       # never the contents


def test_group_readable_token_is_reported(tmp_path, monkeypatch):
    s, _ = _with_env_file(tmp_path, monkeypatch, 0o600)
    token = (tmp_path / "secrets" / "token.json").resolve()
    token.chmod(0o640)
    assert [p for p in secret_file_problems(s) if f"chmod 600 {token}" in p]


def test_owner_only_secrets_pass(tmp_path, monkeypatch):
    s, _ = _with_env_file(tmp_path, monkeypatch, 0o600)
    assert secret_file_problems(s) == []


def test_windows_skips_the_mode_check(tmp_path, monkeypatch):
    s, _ = _with_env_file(tmp_path, monkeypatch, 0o644)
    monkeypatch.setattr(os, "name", "nt")
    assert secret_file_problems(s) == []


def test_run_once_refuses_on_a_readable_secret(tmp_path, monkeypatch):
    _no_db_or_google(monkeypatch)
    cfg = _config(tmp_path, monkeypatch)
    env = (tmp_path / ".env").resolve()
    env.write_text("")
    env.chmod(0o604)
    with pytest.raises(SystemExit, match=re.escape(f"chmod 600 {env}")):
        cli.main(["run-once", "--config", str(cfg)])


def test_check_setup_reports_readable_secrets_and_missing_config(tmp_path, monkeypatch, capsys):
    s, env = _with_env_file(tmp_path, monkeypatch, 0o644)
    s.raw["folders"]["output_root"] = ""

    class NoDB:
        def missing_schema(self):
            raise RuntimeError("no db in this test")

    assert cli._check(s, NoDB(), None) == 1
    out = capsys.readouterr().out
    assert f"chmod 600 {env}" in out
    assert "folders.output_root" in out


def test_login_leaves_the_token_owner_only(tmp_path, monkeypatch):
    import estamp.google_clients as gc
    cfg = _config(tmp_path, monkeypatch)
    token = tmp_path / "secrets" / "token.json"
    token.unlink()

    def fake_credentials(s):
        token.write_text("{}")                                 # written with the default umask, e.g. 0644
        token.chmod(0o644)
        return object()

    class FakeGoogle:
        email = "procurement@zoop.one"

        def __init__(self, s, credentials=None):
            pass

    monkeypatch.setattr(gc, "load_credentials", fake_credentials)
    monkeypatch.setattr(gc, "Google", FakeGoogle)
    assert cli.main(["login", "--config", str(cfg)]) == 0
    assert token.stat().st_mode & 0o077 == 0


def test_only_the_credential_file_run_loads_is_checked(tmp_path, monkeypatch):
    # client_secret.json (browser download, usually 0644) is only read by `login`, and a stray service_account.json
    # is unused under oauth – neither may block run/run-once.
    s, _ = _with_env_file(tmp_path, monkeypatch, 0o600)
    for name in ("client_secret.json", "service_account.json"):
        f = tmp_path / "secrets" / name
        f.write_text("{}")
        f.chmod(0o644)
    s.raw["google"]["service_account_file"] = "secrets/service_account.json"
    assert secret_file_problems(s) == []
    s.raw["google"]["auth"] = "service_account"
    assert [p for p in secret_file_problems(s) if "service_account.json" in p]


def test_recheck_review_also_refuses_before_any_drive_or_db_work(tmp_path, monkeypatch):
    _no_db_or_google(monkeypatch)
    cfg = _config(tmp_path, monkeypatch, env=False)
    with pytest.raises(SystemExit, match="T_OCR_KEY"):
        cli.main(["recheck-review", "--config", str(cfg)])
