"""Slack message formatting: plain-text `lines` (for email) → aligned Slack code-block text."""
from pathlib import Path

from estamp.context import alert_channels, slack_body
from estamp.settings import Settings


def _settings(raw: dict) -> Settings:
    return Settings(raw=raw, base_dir=Path("."))


# ── alert_channels: which delivery channels are actually configured (#2) ──────────────
def test_alert_channels_detects_slack_webhook(monkeypatch):
    monkeypatch.setenv("TEST_HOOK", "https://hooks.slack.test/abc")
    s = _settings({"alerts": {"slack_webhook_env": "TEST_HOOK", "smtp": {"host": "", "to": []}}})
    assert alert_channels(s) == ["slack"]


def test_alert_channels_detects_email_only_when_smtp_host_and_to_set(monkeypatch):
    monkeypatch.delenv("TEST_HOOK", raising=False)
    s = _settings({"alerts": {"slack_webhook_env": "TEST_HOOK", "smtp": {"host": "smtp.test", "to": ["a@b.c"]}}})
    assert alert_channels(s) == ["email"]


def test_alert_channels_empty_when_nothing_configured():
    # The live hole: no webhook env, SMTP host blank → every alert would silently go nowhere.
    s = _settings({"alerts": {"slack_webhook_env": "UNSET_HOOK", "smtp": {"host": "", "to": []}}})
    assert alert_channels(s) == []


def test_send_alerts_logs_loudly_when_there_is_nowhere_to_send(caplog):
    # With alert lines but no channel, delivery must NOT be a silent no-op – it logs ERROR so the
    # failure is at least visible in the terminal and estamp.log (#2).
    import logging

    from estamp.context import Ctx
    s = _settings({"alerts": {"slack_webhook_env": "UNSET_HOOK", "smtp": {"host": "", "to": []}}})
    ctx = Ctx.__new__(Ctx)        # no DB/Google needed – send_alerts only reads settings
    ctx.settings = s
    with caplog.at_level(logging.ERROR, logger="estamp"):
        ctx.send_alerts("[Test] 1 alert", ["ERROR · Allocate: something is stuck"])
    assert any(r.levelno >= logging.ERROR for r in caplog.records), "expected a loud log when no channel is configured"


def test_severity_tokens_become_aligned_labels_and_bullets_are_dropped():
    lines = [
        "Issued 42 · LANs in 50 · errors 1 · waiting 8",
        "",
        "• ERROR · Allocate: LAN 9012345678 (RJ-195): cert mismatch",
        "• WARN · Shortage: KA ₹500: 8 LAN(s) waiting",
    ]
    out = slack_body(lines)
    # summary line is untouched
    assert out.splitlines()[0] == "Issued 42 · LANs in 50 · errors 1 · waiting 8"
    # "• LEVEL · " becomes a fixed-width label so the text aligns in one column (no bullet, no "·")
    assert "ERROR  Allocate: LAN 9012345678 (RJ-195): cert mismatch" in out
    assert "WARN   Shortage: KA ₹500: 8 LAN(s) waiting" in out
    assert "• " not in out and "ERROR · " not in out and "WARN · " not in out
    # labels line up: message text starts at the same column for both
    assert out.splitlines()[2].index("Allocate") == out.splitlines()[3].index("Shortage")


def test_lines_without_a_severity_token_pass_through_unchanged():
    lines = ["Date 2026-10-28", "  KA ₹500: avail 3, cover 0d → LOW"]
    assert slack_body(lines) == "Date 2026-10-28\n  KA ₹500: avail 3, cover 0d → LOW"
