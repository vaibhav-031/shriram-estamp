"""Slack message formatting: plain-text `lines` (for email) → aligned Slack code-block text."""
from estamp.context import slack_body


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
