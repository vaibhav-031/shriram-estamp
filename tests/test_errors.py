"""Central error catalogue + `explain` command (Phase C, first slice). Pure – no DB, no Google."""
import io
from contextlib import redirect_stdout

from estamp import errors, cli


def test_catalogue_has_key_codes():
    for code in ("E101", "E106", "E107", "E111", "E126", "E206", "E302", "E501", "E506"):
        assert code in errors.CATALOGUE, f"{code} missing from catalogue"
        rec = errors.CATALOGUE[code]
        assert rec.when and rec.does and rec.ops


def test_explain_ocr_failure_mentions_meaning_and_action():
    out = errors.explain("E111")
    assert "E111" in out
    assert "OCR" in out
    # ops action from the table ("tell tech if repeating")
    assert "tech" in out.lower()


def test_explain_unknown_code_is_graceful():
    out = errors.explain("E999")
    assert "E999" in out
    # does not raise, returns a usable string
    assert out.strip()


def test_line_formats_one_liner_with_context():
    s = errors.line("E106", "foo.pdf")
    assert "E106" in s
    assert "foo.pdf" in s
    assert s.startswith("✗")


def test_cli_explain_wired():
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(["explain", "E111"])
    assert rc == 0
    assert "E111" in buf.getvalue()
