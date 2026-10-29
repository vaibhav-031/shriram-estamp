"""Every laptop must be signed in to the ONE shared Google account (google.account, e.g. procurement@zoop.one).
A different account would see different (or no) folders, so ops would be working on different Drives."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from estamp import google_clients
from estamp.google_clients import Google, WrongGoogleAccount
from estamp.settings import load_settings

ROOT = Path(__file__).resolve().parents[1]


def _settings(tmp_path, account: str | None, auth: str = "oauth"):
    google = {"auth": auth, "oauth_token_file": str(tmp_path / "token.json")}
    if account is not None:
        google["account"] = account
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"state_config_file": str(ROOT / "state_config.csv"), "google": google}))
    return load_settings(p)


def _signed_in_as(monkeypatch, email):
    calls = []
    monkeypatch.setattr(Google, "account_email", lambda self: calls.append(1) or email)
    return calls


def test_wrong_account_is_refused(tmp_path, monkeypatch):
    _signed_in_as(monkeypatch, "someone@gmail.com")
    with pytest.raises(WrongGoogleAccount) as exc:
        Google(_settings(tmp_path, "procurement@zoop.one"), credentials=object())
    assert "someone@gmail.com" in str(exc.value) and "procurement@zoop.one" in str(exc.value)


def test_expected_account_is_accepted_case_insensitively(tmp_path, monkeypatch):
    _signed_in_as(monkeypatch, "Procurement@Zoop.one")
    Google(_settings(tmp_path, "procurement@zoop.one"), credentials=object())


def test_no_account_configured_means_no_check(tmp_path, monkeypatch):
    calls = _signed_in_as(monkeypatch, "anyone@example.com")
    Google(_settings(tmp_path, None), credentials=object())
    assert calls == []


@pytest.mark.parametrize("value", ["", "  ", "REPLACE_WITH_SHARED_ACCOUNT"])
def test_blank_or_placeholder_account_means_no_check(tmp_path, monkeypatch, value):
    calls = _signed_in_as(monkeypatch, "anyone@example.com")
    Google(_settings(tmp_path, value), credentials=object())
    assert calls == []


def test_service_account_mode_is_not_checked_against_the_shared_login(tmp_path, monkeypatch):
    # A service account's Drive identity is its own …iam.gserviceaccount.com address – it can never equal
    # procurement@, and `login` can't change it. The shared-account rule is for oauth (laptop) logins.
    calls = _signed_in_as(monkeypatch, "bot@proj.iam.gserviceaccount.com")
    Google(_settings(tmp_path, "procurement@zoop.one", auth="service_account"), credentials=object())
    assert calls == []


def _login(tmp_path, monkeypatch, emails: list[str]):
    """Run `login` where each credentials load signs in as the next email in `emails`."""
    from estamp import cli
    s = _settings(tmp_path, "procurement@zoop.one")
    token = tmp_path / "token.json"
    token.write_text("{}")                                   # a cached token from an earlier login
    loads = []
    monkeypatch.setattr(cli, "load_settings", lambda path: s)
    monkeypatch.setattr(google_clients, "load_credentials", lambda settings: loads.append(1) or object())
    seq = iter(emails)
    # Pinned on the instance, not id(self): the first Google dies in __init__ and the next one can reuse its id.
    monkeypatch.setattr(Google, "account_email",
                        lambda self: self.__dict__.setdefault("_fake_email", next(seq)))
    return cli, token, loads


def test_login_replaces_a_cached_wrong_account_token_in_one_go(tmp_path, monkeypatch, capsys):
    # Following the error's advice ("run login") must actually fix it – not just delete and exit.
    cli, token, loads = _login(tmp_path, monkeypatch, ["someone@gmail.com", "procurement@zoop.one"])
    assert cli.main(["login"]) == 0
    assert len(loads) == 2                                   # cached token rejected → fresh browser sign-in
    assert "procurement@zoop.one" in capsys.readouterr().out


def test_login_still_wrong_after_browser_discards_the_token(tmp_path, monkeypatch):
    # `login` must not leave a wrong-account token behind: the next command would silently use it.
    cli, token, _ = _login(tmp_path, monkeypatch, ["someone@gmail.com", "someone@gmail.com"])
    with pytest.raises(SystemExit) as exc:
        cli.main(["login"])
    assert "procurement@zoop.one" in str(exc.value)
    assert not token.exists()
