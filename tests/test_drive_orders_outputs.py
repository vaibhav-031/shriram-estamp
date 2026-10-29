"""Orders / outputs / Drive client / actions hardening (AUDIT H-3, L-2, M-1, M-5 + the dead E105 recheck skip)."""
from __future__ import annotations

import io
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from estamp.google_clients import GSHEET, PDF, XLSX, Google

import samples  # noqa: E402
from fake_google import FakeGoogle  # noqa: E402
from test_all import _ctx, _vendor_folder, e2e, env, settings  # noqa: F401,E402  (fixtures)


def _xlsx(rows: list[list]) -> bytes:
    """A real .xlsx built with openpyxl. A cell given as '=…' is saved as a formula WITHOUT a cached value –
    exactly what LibreOffice-headless / some exporters produce."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "MH"
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _reader(settings, g):
    return SimpleNamespace(google=g, settings=settings)


# ============================================================ recheck_review: E107 papers stay put

@e2e
def test_recheck_review_leaves_e107_pairing_papers_for_a_reupload(env):
    """E107 papers read fine on their own (F3+B3 is a valid pair) – recheck must still not stock them: their
    pairing is suspect, so only a re-upload can clear them."""
    from estamp.actions import recheck_review
    from estamp.cycle import run_cycle
    s, db, g = env
    f = lambda n: samples.scanned_front("RAJASTHAN", f"S 25420{n}", 195)
    b = lambda: samples.scanned_back("RAJASTHAN")
    g.add("broken.pdf", PDF, _vendor_folder(g, s), samples.merged([f(1), b(), f(2), f(3), b(), f(4)]))
    assert run_cycle(_ctx(s, db, g))
    before = db.q("SELECT id, notes FROM papers WHERE status='NEEDS_REVIEW' ORDER BY id")
    # Intake stamps E120 in front (no cert on the unit), so the E107 marker is NOT at the start of the note.
    assert len(before) == 2 and all("E107 pairing broken" in r["notes"] for r in before), before
    assert recheck_review(_ctx(s, db, g)) == (0, 2)
    assert db.q("SELECT id, notes FROM papers WHERE status='NEEDS_REVIEW' ORDER BY id") == before
