"""Windows: a console or pipe (e.g. under `make`) is cp1252, which has no ✗ / ✓ / … – logging or printing an ops
line must never fail there, and the log file is always UTF-8 whatever the machine's locale."""
import io
import logging
import sys

import pytest

from estamp import context
from estamp.context import log, setup_logging
from estamp.settings import Settings


@pytest.fixture
def cp1252_console(monkeypatch, tmp_path):
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    err = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    saved = log.handlers[:]
    log.handlers.clear()
    setup_logging(Settings(raw={}, base_dir=tmp_path))
    yield out, err
    for h in log.handlers:
        h.close()
    log.handlers[:] = saved


def _text(stream):
    stream.flush()
    return stream.buffer.getvalue().decode("cp1252")


def test_ops_line_with_cross_mark_logs_on_cp1252_console(cp1252_console):
    _, err = cp1252_console
    log.warning("Intake: ✗ [E106] Traditional file with odd page count … 'page25.pdf'")
    text = _text(err)
    assert "Logging error" not in text
    assert "[E106] Traditional file with odd page count" in text


def test_check_setup_style_print_survives_cp1252_console(monkeypatch):
    # Patched here, not in a fixture: pytest re-installs its own sys.stdout between setup and the test body.
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    context.safe_console()
    print("  ✓ Database – ok")      # check-setup prints ✓ / ✗ – a crash here would kill the command
    assert "? Database – ok" in _text(out)


def test_log_file_is_utf8(cp1252_console, tmp_path):
    log.warning("✗ [E106] page25.pdf")
    for h in log.handlers:
        h.flush()
    assert "✗ [E106] page25.pdf" in (tmp_path / "logs" / "estamp.log").read_text(encoding="utf-8")
    assert all(getattr(h, "encoding", "utf-8") == "utf-8" for h in log.handlers if isinstance(h, logging.FileHandler))
