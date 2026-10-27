"""Run: ESTAMP_TEST_DB_URL=postgresql://… python -m pytest -q tests/
The end-to-end tests create and drop their own schema (estamp_test); they never touch anything else.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from estamp.context import Ctx  # noqa: E402
from estamp.extract import (CertMatcher, StateResolver, analyze_paper, normalize_ocr,  # noqa: E402
                            parse_order_tabs)
from estamp.google_clients import FOLDER, GSHEET, PDF  # noqa: E402
from estamp.settings import load_settings  # noqa: E402

import samples  # noqa: E402
from fake_google import FakeGoogle  # noqa: E402


@pytest.fixture(scope="session")
def settings():
    return load_settings(ROOT / "config.yaml")


# ============================================================ pure logic

def test_projection_loaded(settings):
    assert len(settings.states) == 27
    assert sum(s.daily_qty for s in settings.states.values()) == 10790
    assert not settings.states["KL"].active and not settings.states["SK"].active
    assert settings.states["RJ"].amounts == [195]


@pytest.mark.parametrize("raw,code", [("KARTNATAKA", "KA"), ("ANDRA PRADESH", "AP"), ("UTTARPRADESH", "UP"),
                                      ("DELHI NCR", "DL"), ("KOLKATA", "WB"), ("JAMMU & KASHMIR", "JK"),
                                      ("gujarat", "GJ"), ("MH", "MH"), ("Telangana", "TS"), ("Sheet1", "")])
def test_state_aliases(settings, raw, code):
    assert StateResolver(settings.states).resolve(raw) == code


def test_cert_and_amount(settings):
    r = StateResolver(settings.states)
    m = CertMatcher(settings.get("intake.cert_patterns"), r)
    t = normalize_ocr("Certificate No. : I N-MH 9000000000000 1A\nStamp Duty Amount(Rs.) : 200 (Two Hundred)")
    a = analyze_paper(t, "", settings, m, lambda c: False)
    assert a.ok, a.issues
    assert (a.cert, a.state, a.amount) == ("IN-MH90000000000001A", "MH", 200)
    tg = analyze_paper("Certificate No. IN-TG12345678901234B Stamp Duty Amount(Rs.) 100", "", settings, m, lambda c: False)
    assert tg.state == "TS" and tg.ok
    bad = analyze_paper("Certificate No. IN-MH12345678901234B Stamp Duty Amount(Rs.) 100", "", settings, m, lambda c: False)
    assert "not an allowed amount" in bad.issues[0]
    kl = analyze_paper("IN-KL12345678901234B Stamp Duty Amount 200", "", settings, m, lambda c: False)
    assert any("inactive" in i for i in kl.issues)
    two = analyze_paper("IN-MH12345678901234B and IN-MH12345678901235C Stamp Duty Amount 200", "", settings, m, lambda c: False)
    assert any("more than one" in i for i in two.issues)
    dup = analyze_paper("IN-MH12345678901234B Stamp Duty Amount 200", "", settings, m, lambda c: True)
    assert any("duplicate" in i for i in dup.issues)


def test_order_sheets(settings):
    r = StateResolver(settings.states)
    res = {n: parse_order_tabs([(n, fn())], n, settings, r) for n, fn in samples.SHEETS.items()}
    mh = res["TEST_Shriram_MH_LANs_26Oct"]
    assert len(mh.valid) == 12 and len(mh.duplicates) == 1 and len(mh.invalid) == 1
    ka = res["TEST_Shriram_Karnataka_LANs_26Oct"]
    assert len(ka.valid) == 10 and {o.state for o in ka.valid} == {"KA"} and {o.denomination for o in ka.valid} == {500}
    tn = res["TEST_Shriram_TN_LANs_26Oct"]
    assert len(tn.valid) == 10 and {o.state for o in tn.valid} == {"TN"}
    gj = res["TEST_Shriram_GJ_LANs_26Oct"]
    assert len(gj.valid) == 11 and len(gj.invalid) == 1 and "250" in gj.invalid[0]
    up = res["TEST_Shriram_UP_LANs_26Oct"]
    assert len(up.valid) == 10 and up.valid[0].row_no == 5


# ============================================================ end to end

DB_URL = os.environ.get("ESTAMP_TEST_DB_URL")
e2e = pytest.mark.skipif(not DB_URL, reason="set ESTAMP_TEST_DB_URL to run end-to-end tests")


@pytest.fixture
def env(settings):
    import psycopg
    from estamp.db import DB
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS estamp_test CASCADE")
    settings.raw["db"]["schema"] = "estamp_test"
    settings.raw["dry_run"] = False
    settings.raw["orders"]["settle_minutes"] = 0
    settings.raw["intake"]["settle_minutes"] = 0
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
    return Ctx(settings, db, g)


def _stock(g, s, state, amount, n, start=1):
    pages = [samples.paper_page(samples.cert(state, start + i), amount) for i in range(n)]
    return g.add(f"vendor_{state}_{amount}_{start}.pdf", PDF, s.folder("upload_inbox"), samples.merged(pages))


@e2e
def test_full_flow(env):
    from estamp.cycle import run_cycle
    s, db, g = env
    # ---- orders (MH oldest so the GJ sheet's MH LAN creates a conflict)
    for age, (name, fn) in zip([50, 40, 30, 20, 10], samples.SHEETS.items()):
        g.add(name, GSHEET, s.folder("orders_incoming"), tabs={name: fn()}, age_min=age)
    # ---- vendor papers (merged PDFs, auto-routed by cert state + amount)
    _stock(g, s, "MH", 200, 14)
    ka = _stock(g, s, "KA", 500, 10)
    _stock(g, s, "TN", 200, 6)
    _stock(g, s, "GJ", 300, 10)
    _stock(g, s, "UP", 100, 10)
    g.add("vendor_KA_copy.pdf", PDF, s.folder("upload_inbox"), g.files[ka]["data"])          # exact duplicate upload
    bad = samples.merged([samples.paper_page(samples.cert("MH", 900), 100),                  # wrong amount for MH
                          samples.paper_page(samples.cert("MH", 1), 200),                    # duplicate cert
                          samples.paper_page("", 200),                                       # no cert
                          samples.paper_page(samples.cert("KL", 1), 200)])                   # inactive state
    g.add("vendor_mixed_bad.pdf", PDF, s.folder("upload_inbox"), bad)
    g.add("notes.txt", "text/plain", s.folder("upload_inbox"), b"hello")

    ctx = _ctx(s, db, g)
    assert run_cycle(ctx)

    st = {r["status"]: r["n"] for r in db.q("SELECT status, count(*) n FROM orders GROUP BY 1")}
    assert st == {"DONE": 47, "PENDING": 4, "CONFLICT": 2}, st
    assert db.q1("SELECT count(*) n FROM papers WHERE status='NEEDS_REVIEW'")["n"] == 4
    reasons = " | ".join(r["notes"] for r in db.q("SELECT notes FROM papers WHERE status='NEEDS_REVIEW'"))
    for needle in ["not an allowed amount", "duplicate certificate", "no certificate / serial number", "inactive"]:
        assert needle in reasons, reasons
    assert db.q1("SELECT count(*) n FROM uploads WHERE status='DUPLICATE'")["n"] == 1

    # nothing allocated twice, every allocation's file really sits in Output as <LAN>.pdf
    allocs = db.q("SELECT * FROM allocations")
    assert len(allocs) == 47 and len({a["paper_id"] for a in allocs}) == 47
    for a in allocs:
        path = g.path_of(a["pdf_file_id"])
        assert path.endswith(f"{a['state']}/{a['state']}-{a['denomination']} PDFs/{a['lan']}.pdf"), path
        assert g.path_of(a["backup_file_id"]).startswith("backup_root/")
    # auto-nesting created Inventory/<STATE>/<AMOUNT>/ and spare MH papers wait there named by cert
    inv = [g.path_of(f["id"]) for f in g.files.values() if f["mimeType"] == PDF and "inventory_root/" in g.path_of(f["id"])]
    assert len(inv) == 3 and all(p.startswith("inventory_root/MH/200/IN-MH") for p in inv), inv
    # output sheets: one per state/amount for today
    outs = {g.files[sid]["name"]: rows for sid, tabs in g.sheets_data.items() if sid in g.files
            and g.files[sid]["name"].count("-") == 1 for rows in tabs.values()}
    assert len(outs["MH-200"]) == 1 + 11 and len(outs["TN-200"]) == 1 + 6
    # second OCR: every issued PDF was re-read and its printed cert matches the ledger
    for row in outs["MH-200"][1:]:
        assert row[1].startswith("IN-MH") and len(row) == 6, row
    assert db.q1("SELECT count(*) n FROM allocations WHERE verified_cert IS DISTINCT FROM cert_no")["n"] == 0
    # every order file left the inbox; the non-PDF and the duplicate upload went to Needs Review
    assert g.list_children(s.folder("orders_incoming")) == []
    assert g.list_children(s.folder("upload_inbox")) == []
    # dashboard
    ctl = g.sheets_data[s.get("control_sheet_id")]
    assert {"Summary", "Stock", "Needs Review", "Exceptions", "Log"} <= set(ctl)
    tn_row = [r for r in ctl["Stock"] if r[0] == "TN"][0]
    assert tn_row[4] == 0 and tn_row[6] == 4 and tn_row[13] == "SHORT"
    assert any("TN ₹200: 4 LAN(s) waiting" in a for a in ctx.alerts)

    # ---- cycle 2: ops actions + new stock
    rv = db.q1("SELECT id FROM papers WHERE notes LIKE %s", ("%no certificate / serial number%",))["id"]
    ctl["Review Actions"].append([str(rv), "APPROVE", samples.cert("MH", 777), "MH", "200"])
    ctl["Order Actions"].append(["TEST-MH-000005", "KEEP_STATE", "MH"])
    ctl["Order Actions"].append(["TEST-UP-000010", "CANCEL", ""])
    _stock(g, s, "TN", 200, 4, start=50)
    ctx2 = _ctx(s, db, g)
    assert run_cycle(ctx2)

    assert [r[5] for r in ctl["Review Actions"][1:]][0].startswith("DONE: approved")
    results = [r[3] for r in ctl["Order Actions"][1:]]
    assert results[0].startswith("DONE: MH kept") and results[1].startswith("DONE: cancelled, paper returned"), results
    st = {r["status"]: r["n"] for r in db.q("SELECT status, count(*) n FROM orders GROUP BY 1")}
    assert st == {"DONE": 51, "CANCELLED": 2}, st   # +4 TN, +MH-000005, −UP-000010; GJ copy of MH-000005 cancelled
    up_paper = db.q1("SELECT p.* FROM papers p JOIN orders o ON o.paper_id = p.id WHERE o.lan='TEST-UP-000010'")
    assert up_paper["status"] == "AVAILABLE" and g.path_of(up_paper["drive_file_id"]).startswith("inventory_root/UP/100/")
    assert outs["UP-100"][-1][5].startswith("CANCELLED")


@e2e
def test_crash_recovery_and_dry_run(env):
    from estamp.cycle import run_cycle
    s, db, g = env
    g.add("TEST_Shriram_MH_LANs_26Oct", GSHEET, s.folder("orders_incoming"), tabs={"x": samples.mh()})
    _stock(g, s, "MH", 200, 2)
    s.raw["dry_run"] = True
    ctx = _ctx(s, db, g)
    assert run_cycle(ctx)
    assert db.q1("SELECT count(*) n FROM orders")["n"] == 0 and db.q1("SELECT count(*) n FROM papers")["n"] == 0
    assert len(g.list_children(s.folder("upload_inbox"))) == 1, "dry run must not move anything"
    kinds = {r[0] for r in ctx.preview}
    assert {"PAPER", "ORDER", "ALLOCATE"} <= kinds
    alloc = [r for r in ctx.preview if r[0] == "ALLOCATE"]
    assert len(alloc) == 12 and sum(r[7].startswith("OK") for r in alloc) == 2, alloc   # 2 papers for 12 LANs
    assert "Dry Run Preview" in g.sheets_data[s.get("control_sheet_id")]

    s.raw["dry_run"] = False
    assert run_cycle(_ctx(s, db, g))
    # simulate a crash mid-allocation: an order left ALLOCATING 30 min ago
    db.q("UPDATE orders SET status='ALLOCATING', updated_at=now()-interval '30 minutes' WHERE id=(SELECT min(id) FROM orders WHERE status='PENDING')")
    ctx3 = _ctx(s, db, g)
    run_cycle(ctx3)
    assert db.q1("SELECT count(*) n FROM orders WHERE status='ERROR'")["n"] == 1
    assert any("stuck mid-allocation" in a for a in ctx3.alerts)


@e2e
def test_issue_time_ocr_catches_swapped_file(env):
    """A paper's file is swapped after intake (wrong PDF in the folder). The second OCR must stop it."""
    from estamp.cycle import run_cycle
    s, db, g = env
    _stock(g, s, "MH", 200, 2)
    assert run_cycle(_ctx(s, db, g))                       # intake only, no orders yet
    papers = db.q("SELECT * FROM papers WHERE status='AVAILABLE' ORDER BY added_at, id")
    assert len(papers) == 2
    first = papers[0]
    g.files[first["drive_file_id"]]["data"] = samples.paper_page(samples.cert("MH", 555), 200)   # tamper
    g.add("TEST_Shriram_MH_LANs_26Oct", GSHEET, s.folder("orders_incoming"),
          tabs={"x": [samples.H, ["1", "LAN-A", "c", "b", "MH", "200"], ["2", "LAN-B", "c", "b", "MH", "200"]]})
    ctx = _ctx(s, db, g)
    run_cycle(ctx)
    st = {r["lan"]: r["status"] for r in db.q("SELECT lan, status FROM orders")}
    assert sorted(st.values()) == ["DONE", "ERROR"], st
    bad = db.q1("SELECT * FROM papers WHERE id=%s", (first["id"],))
    assert bad["status"] == "ERROR" and "ledger expected" in bad["notes"]
    assert "Cert mismatch" in g.path_of(bad["drive_file_id"])
    out = [rows for sid, tabs in g.sheets_data.items() if sid in g.files and g.files[sid]["name"] == "MH-200"
           for rows in tabs.values()][0]
    assert len(out) == 2 and out[1][1].startswith("IN-MH")    # only the good one was issued
    assert any("ledger expected" in a for a in ctx.alerts)


@e2e
def test_inbox_accepts_uploaded_folders(env):
    """Ops drag whole folders (any name, nested) into the inbox – papers inside are processed, folders archived."""
    from estamp.cycle import run_cycle
    s, db, g = env
    inbox = s.folder("upload_inbox")
    rj = g.folder("RJ", inbox)
    batch = g.folder("vendor batch 2", rj)
    g.add("p1.pdf", PDF, rj, samples.paper_page(samples.cert("RJ", 1), 195))
    g.add("p2.pdf", PDF, batch, samples.merged([samples.paper_page(samples.cert("RJ", 2), 195),
                                                samples.paper_page(samples.cert("RJ", 3), 195)]))
    g.add("loose.pdf", PDF, inbox, samples.paper_page(samples.cert("MH", 1), 200))
    assert run_cycle(_ctx(s, db, g))
    stocked = {r["cert_no"]: r["state"] for r in db.q("SELECT cert_no, state FROM papers WHERE status='AVAILABLE'")}
    assert stocked == {samples.cert("RJ", 1): "RJ", samples.cert("RJ", 2): "RJ", samples.cert("RJ", 3): "RJ",
                       samples.cert("MH", 1): "MH"}, stocked
    assert g.list_children(inbox) == [], "inbox should be empty – files processed, folders archived"
    paths = [g.path_of(f["id"]) for f in g.files.values() if f["name"] == "RJ"]
    assert paths and paths[0].startswith("upload_archive/"), paths


@e2e
def test_volume_no_double_issue(env):
    """~1,000 LANs over four state + stamp duty groups (TN short by 50): every LAN gets at most one paper,
    no paper is issued twice, and the LANs without stock stay PENDING. Guard for every phase (definition of done)."""
    from estamp.cycle import run_cycle
    from fake_apis import FakeOcr
    s, db, g = env
    groups = [("MH", 200, 300, 300), ("UP", 100, 300, 300), ("GJ", 300, 200, 200), ("TN", 200, 200, 150)]
    rows = [samples.H]
    for st, amt, lans, papers in groups:
        rows += [[str(len(rows)), f"VOL-{st}-{i:05d}", "c", "b", st, str(amt)] for i in range(lans)]
        _stock(g, s, st, amt, papers)
    g.add("volume_orders", GSHEET, s.folder("orders_incoming"), tabs={"x": rows})

    for _ in range(3):                       # a cycle is time-boxed; repeat until nothing is left to issue
        assert run_cycle(Ctx(s, db, g, ocr=FakeOcr()))   # fake reader: independent of Drive OCR / text layer
        if not db.pending_groups() or all(grp["avail"] == 0 for grp in db.pending_groups()):
            break

    st = {r["status"]: r["n"] for r in db.q("SELECT status, count(*) n FROM orders GROUP BY 1")}
    assert st == {"DONE": 950, "PENDING": 50}, st
    assert {r["state"] for r in db.q("SELECT state FROM orders WHERE status='PENDING'")} == {"TN"}
    allocs = db.q("SELECT * FROM allocations WHERE cancelled_at IS NULL")
    assert len(allocs) == 950
    assert len({a["paper_id"] for a in allocs}) == 950, "a paper was issued twice"
    assert len({a["order_id"] for a in allocs}) == 950, "a LAN got two papers"
    assert len({a["pdf_file_id"] for a in allocs}) == 950
    assert db.q1("SELECT count(*) n FROM papers WHERE status='ASSIGNED'")["n"] == 950
    assert db.q1("SELECT count(*) n FROM papers WHERE status NOT IN ('ASSIGNED')")["n"] == 0
    for a in allocs:
        assert g.path_of(a["pdf_file_id"]).endswith(f"/{a['lan']}.pdf")


@e2e
def test_intake_and_issue_check_read_through_the_context_clients(env):
    """Intake splits and reads, and the issue check reads, only through the run context's split + OCR clients."""
    from estamp.cycle import run_cycle
    from fake_apis import FakeOcr, FakeSplitter
    s, db, g = env
    _stock(g, s, "MH", 200, 2)
    g.add("orders", GSHEET, s.folder("orders_incoming"),
          tabs={"x": [samples.H, ["1", "LAN-A", "c", "b", "MH", "200"]]})
    ocr, splitter = FakeOcr(), FakeSplitter()
    assert run_cycle(Ctx(s, db, g, ocr=ocr, splitter=splitter))
    assert splitter.calls == [("vendor_MH_200_1.pdf", 1)]
    assert sorted(ocr.calls) == ["LAN-A.pdf_p1", "vendor_MH_200_1.pdf_p1", "vendor_MH_200_1.pdf_p2"]
    a = db.q1("SELECT * FROM allocations")
    assert a["lan"] == "LAN-A" and a["verified_via"] == "fake"


@e2e
@pytest.mark.xfail(strict=True, reason="ticket A-08 not built: today one failed page goes to Needs Review on its own")
def test_ocr_failure_keeps_the_whole_upload_in_the_inbox(env):
    """A-08: a read failure leaves the whole upload in the Upload Inbox for the next cycle – nothing half-stocked."""
    from estamp.cycle import run_cycle
    from fake_apis import FakeOcr
    s, db, g = env
    up = _stock(g, s, "MH", 200, 3)
    assert run_cycle(Ctx(s, db, g, ocr=FakeOcr(fail={"vendor_MH_200_1.pdf_p2": RuntimeError("OCR API HTTP 503")})))
    assert db.q1("SELECT count(*) n FROM papers")["n"] == 0
    assert [f["id"] for f in g.list_children(s.folder("upload_inbox"))] == [up]


# ============================================================ split API (replays docs/api-samples/split)

SPLIT_SAMPLES = ROOT / "docs" / "api-samples" / "split"


class _Resp:
    def __init__(self, status, content, ctype="application/octet-stream"):
        self.status_code, self.content, self.headers = status, content, {"content-type": ctype}

    def json(self):
        import json
        return json.loads(self.content)


@pytest.fixture
def split_api(settings, monkeypatch):
    """split_provider=zoop_api with requests.post replaced by a recorder that returns a scripted response."""
    import estamp.http_api as http_api
    import estamp.ocr_split as ocr_split
    monkeypatch.setitem(settings.raw["intake"], "split_provider", "zoop_api")
    monkeypatch.setattr(http_api, "_sleep", lambda s: None)          # retries/backoff without real waiting
    monkeypatch.setattr(http_api, "_limiters", {})
    calls, reply = [], {}

    def post(url, **kw):
        calls.append({"url": url, **kw})
        if "raise" in reply:
            raise reply["raise"]
        if "seq" in reply:
            return reply["seq"].pop(0)
        return reply["resp"]

    monkeypatch.setattr(ocr_split.requests, "post", post)
    return calls, reply


def _pages(data: bytes) -> int:
    import io
    from pypdf import PdfReader
    return len(PdfReader(io.BytesIO(data)).pages)


def test_split_api_digital_one_page_per_part(settings, split_api):
    from estamp.ocr_split import pdf_text, split_pdf
    calls, reply = split_api
    reply["resp"] = _Resp(200, (SPLIT_SAMPLES / "response_splitSize1.zip").read_bytes())
    pdf = (SPLIT_SAMPLES / "test_input_4pages.pdf").read_bytes()
    parts = split_pdf(pdf, "vendor.pdf", settings)
    assert [(p.page_from, p.page_to) for p in parts] == [(1, 1), (2, 2), (3, 3), (4, 4)]
    assert all(_pages(p.data) == 1 for p in parts)
    assert samples.cert("MH", 3) in pdf_text(parts[2].data)
    (c,) = calls
    assert c["url"] == "https://live.zoopsign.com/api/pdf-utils/split"
    assert c["data"] == {"ranges[0][startPage]": "1", "ranges[0][endPage]": "4", "splitSize": "1"}
    assert c["files"]["file"][0] == "vendor.pdf" and c["files"]["file"][1] == pdf
    assert "headers" not in c or not c["headers"]          # no auth header
    assert c["timeout"]


def test_split_api_traditional_two_pages_per_part(settings, split_api):
    from estamp.ocr_split import split_pdf
    calls, reply = split_api
    reply["resp"] = _Resp(200, (SPLIT_SAMPLES / "response_splitSize2.zip").read_bytes())
    parts = split_pdf((SPLIT_SAMPLES / "test_input_4pages.pdf").read_bytes(), "scan.pdf", settings, split_size=2)
    assert [(p.page_from, p.page_to) for p in parts] == [(1, 2), (3, 4)]
    assert all(_pages(p.data) == 2 for p in parts)
    assert calls[0]["data"]["splitSize"] == "2"


def _zip_without(sample: str, drop: str) -> bytes:
    import io, zipfile
    src, buf = zipfile.ZipFile(SPLIT_SAMPLES / sample), io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for n in src.namelist():
            if n != drop:
                out.writestr(n, src.read(n))
    return buf.getvalue()


@pytest.mark.parametrize("make_reply,needle", [
    (lambda: {"resp": _Resp(500, (SPLIT_SAMPLES / "response_not_pdf.json").read_bytes(), "application/json")},
     "HTTP 500: Failed to parse PDF document"),
    (lambda: {"resp": _Resp(200, b"<html>gateway error</html>", "text/html")}, "not a zip"),
    (lambda: {"resp": _Resp(200, _zip_without("response_splitSize1.zip", "-3-3.pdf"))}, "don't cover pages 1-4"),
    (lambda: {"raise": requests.Timeout()}, "timed out"),
    (lambda: {"raise": requests.ConnectionError("Max retries exceeded with url ... pool=<urllib3 …>")},
     "unreachable"),
    (lambda: {"resp": _Resp(502, b'{"error":"upstream"}', "application/json")}, 'HTTP 502: {"error":"upstream"}'),
])
def test_split_api_failures_raise_clear_errors(settings, split_api, make_reply, needle):
    from estamp.ocr_split import split_pdf
    _, reply = split_api
    reply.update(make_reply())
    with pytest.raises(RuntimeError, match=needle):
        split_pdf((SPLIT_SAMPLES / "test_input_4pages.pdf").read_bytes(), "vendor.pdf", settings)


def test_split_api_is_rate_limited_and_retried(settings, split_api):
    """A-07: a 503 blip is retried through the shared split limiter and then succeeds."""
    import estamp.http_api as http_api
    from estamp.ocr_split import split_pdf
    calls, reply = split_api
    reply["seq"] = [_Resp(503, b"{}", "application/json"),
                    _Resp(200, (SPLIT_SAMPLES / "response_splitSize1.zip").read_bytes())]
    parts = split_pdf((SPLIT_SAMPLES / "test_input_4pages.pdf").read_bytes(), "vendor.pdf", settings)
    assert len(parts) == 4 and len(calls) == 2
    assert [k[0] for k in http_api._limiters] == ["split"]                      # its own limiter


def test_ocr_api_calls_go_through_the_shared_ocr_limiter(settings, split_api, monkeypatch):
    """A-07: OCR calls (intake and issue check use the same adapter) are rate-limited and retried, on their own
    limiter – separate from the split API's."""
    import estamp.http_api as http_api
    from estamp.ocr_split import _ocr_zoop
    calls, reply = split_api
    monkeypatch.setitem(settings.raw["zoop_api"]["ocr"], "url", "https://ocr.example.test/do")
    monkeypatch.setenv("ZOOP_OCR_API_KEY", "test-key-not-real")
    reply["seq"] = [_Resp(503, b"{}", "application/json"),
                    _Resp(200, b'{"data":{"text":"x","certificate_number":"IN-MH90000000000001A"}}',
                          "application/json")]
    r = _ocr_zoop(b"%PDF-fake", "p1", settings)
    assert r.cert == "IN-MH90000000000001A" and len(calls) == 2
    assert any(k[0] == "ocr" for k in http_api._limiters) and not any(k[0] == "split" for k in http_api._limiters)


def test_split_api_rejects_a_pdf_with_no_pages_without_calling(settings, split_api):
    """A 0-page PDF must fail loudly (→ ERROR, file stays for a human), never count as 'processed, 0 papers'."""
    import io
    from pypdf import PdfWriter
    from estamp.ocr_split import split_pdf
    calls, _ = split_api
    buf = io.BytesIO()
    PdfWriter().write(buf)
    with pytest.raises(RuntimeError, match="no pages"):
        split_pdf(buf.getvalue(), "empty.pdf", settings)
    assert calls == []


def test_split_api_passes_odd_last_part_through(settings, split_api):
    """The API does not reject an odd page count with splitSize=2 – it returns a 1-page last part.
    The adapter reports it faithfully; rejecting the upload (E106) is Phase B's job, before the call."""
    from estamp.ocr_split import split_pdf
    _, reply = split_api
    reply["resp"] = _Resp(200, (SPLIT_SAMPLES / "response_odd3_splitSize2.zip").read_bytes())
    parts = split_pdf((SPLIT_SAMPLES / "test_input_3pages.pdf").read_bytes(), "odd.pdf", settings, split_size=2)
    assert [(p.page_from, p.page_to) for p in parts] == [(1, 2), (3, 3)]


@e2e
def test_intake_via_split_api_keeps_front_and_back_together(env, monkeypatch):
    """Through the split API (fake that follows the captured contract): a scanned upload is sent with splitSize=2
    and each paper keeps its front + back."""
    import io, zipfile
    import estamp.ocr_split as ocr_split
    from estamp.cycle import run_cycle
    s, db, g = env
    monkeypatch.setitem(s.raw["intake"], "split_provider", "zoop_api")
    sizes = []

    def fake_api(url, files, data, timeout):
        sizes.append(data["splitSize"])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for p in ocr_split._split_local(files["file"][1], int(data["splitSize"])):
                z.writestr(f"-{p.page_from}-{p.page_to}.pdf", p.data)
        return _Resp(200, buf.getvalue())

    monkeypatch.setattr(ocr_split.requests, "post", fake_api)
    g.add("scan.pdf", PDF, s.folder("upload_inbox"), samples.merged([
        samples.scanned_front("RAJASTHAN", "S 254201", 195), samples.scanned_back("RAJASTHAN"),
        samples.scanned_front("RAJASTHAN", "S 254202", 195), samples.scanned_back("RAJASTHAN")]))
    assert run_cycle(_ctx(s, db, g))
    assert sizes == ["2"]
    rows = db.q("SELECT cert_no, page_from, page_to, status FROM papers ORDER BY page_from")
    assert [(r["cert_no"], r["page_from"], r["page_to"], r["status"]) for r in rows] == [
        ("RJ-S254201", 1, 2, "AVAILABLE"), ("RJ-S254202", 3, 4, "AVAILABLE")], rows


@e2e
def test_traditional_page_with_a_text_layer_is_never_stocked_as_one_page(env):
    """A 'searchable' scan has a text layer, so its upload looks digital. A page carrying a traditional
    STATE-SERIAL in a digital upload must not be stocked as a 1-page paper without its back → Needs Review (E105)."""
    from estamp.cycle import run_cycle
    s, db, g = env
    g.add("searchable_scan.pdf", PDF, s.folder("upload_inbox"), samples.merged([
        samples.physical_front("RAJASTHAN", "S 254201", 195), samples.physical_back("RAJASTHAN"),
        samples.paper_page(samples.cert("MH", 1), 200)]))
    assert run_cycle(_ctx(s, db, g))
    rows = {r["page_from"]: r for r in db.q("SELECT * FROM papers")}
    assert rows[1]["status"] == "NEEDS_REVIEW" and "E105" in rows[1]["notes"], rows[1]
    assert rows[3]["status"] == "AVAILABLE"                                   # the real e-stamp is fine
    assert db.q1("SELECT count(*) n FROM papers WHERE status='AVAILABLE' AND cert_no LIKE 'RJ-%%'")["n"] == 0


# ============================================================ Phase B: paper type + pairing (pure rules)

@pytest.mark.parametrize("chars,expected", [
    ([500, 480, 512], "digital"),                 # e-stamp PDFs: every page has a real text layer
    ([0, 0, 3, 0], "traditional"),                # scans: no / negligible text layer
    ([500, 0], "mixed"),                          # digital and scanned in one upload
    ([39, 41], "mixed"),                          # either side of the threshold (40)
    ([], "mixed"),                                # nothing to decide on
])
def test_paper_type_from_text_layer(chars, expected):
    from estamp.extract import paper_type
    assert paper_type(chars, 40) == expected


@pytest.mark.parametrize("pairs,expected", [
    ([(1, 0), (1, 0), (1, 0)], None),             # every front has one serial, no back has one
    ([(1, 0), (0, 1), (1, 0)], 1),                # back page missing → the next front lands on the back side
    ([(0, 0), (1, 0)], 0),                        # break at the first pair
    ([(1, 0), (1, 1)], 1),                        # serial on a back page
    ([(1, 0), (2, 0)], 1),                        # two serials on one front
])
def test_first_broken_pair(pairs, expected):
    from estamp.extract import first_broken_pair
    assert first_broken_pair(pairs) == expected


def test_real_rajasthan_ocr_text(settings):
    """OCR text captured from the vendor's real scan (front + back of a physical RJ stamp paper)."""
    from estamp.extract import identify
    r = StateResolver(settings.states); m = CertMatcher(settings.get("intake.cert_patterns"), r)
    front = "INDIA NON JUDICIAL\nरु.1000\nONE THOUSAND RUPEES\nRs.1000\nराजस्थान RAJASTHAN\n24 SEP 2026\nVIONI\nS 254201"
    back = "245117\n24.09.2026\nKAABIL FINANCE PRIVATE LIMITED\nRAJASTHAN\nलाइसेन्स नं.- 28 / 2024\nरकम 1000/-"
    assert [h.cert for h in identify(normalize_ocr(front), settings, m, r)] == ["RJ-S254201"]
    assert identify(normalize_ocr(back), settings, m, r) == []
    a = analyze_paper(front + "\n" + back, "", settings, m, lambda c: False)
    assert (a.cert, a.state, a.amount) == ("RJ-S254201", "RJ", 1000)


@e2e
def test_physical_papers_front_and_back(env):
    """Scanned traditional upload (front + back pairs) – split in pairs, routed, issued, issue check passes.
    The last pair's serial is smudged: that pair breaks pairing and goes to Needs Review, still 2 pages."""
    from estamp.cycle import run_cycle
    s, db, g = env
    pages = []
    for serial in ["S 254201", "S 254202", "AB 100003", "(serial smudged)"]:
        pages += [samples.scanned_front("RAJASTHAN", serial, 195), samples.scanned_back("RAJASTHAN")]
    g.add("vendor_scan.pdf", PDF, s.folder("upload_inbox"), samples.merged(pages))
    g.add("orders", GSHEET, s.folder("orders_incoming"),
          tabs={"x": [["LOAN NO", "STATE"], ["RJLAN0001", "RAJASTHAN"], ["RJLAN0002", "RAJASTHAN"]]})
    ctx = _ctx(s, db, g)
    assert run_cycle(ctx)
    smudged = db.q("SELECT * FROM papers WHERE cert_no IS NULL")
    assert len(smudged) == 1 and (smudged[0]["page_from"], smudged[0]["page_to"]) == (7, 8), smudged
    assert smudged[0]["status"] == "NEEDS_REVIEW" and "E107" in smudged[0]["notes"]
    papers = {r["cert_no"]: r for r in db.q("SELECT * FROM papers WHERE cert_no IS NOT NULL")}
    assert set(papers) == {"RJ-S254201", "RJ-S254202", "RJ-AB100003"}, set(papers)
    assert all(papers[k]["page_to"] - papers[k]["page_from"] == 1 for k in papers)
    done = db.q("SELECT o.lan, a.cert_no, a.verified_cert, a.pdf_file_id FROM orders o JOIN allocations a ON a.order_id=o.id")
    assert len(done) == 2 and all(d["verified_cert"] == d["cert_no"] for d in done), done
    from pypdf import PdfReader
    import io as _io
    assert all(len(PdfReader(_io.BytesIO(g.files[d["pdf_file_id"]]["data"])).pages) == 2 for d in done)
    left = [g.path_of(f["id"]) for f in g.files.values() if "inventory_root/RJ/195/" in g.path_of(f["id"])]
    assert len(left) == 1 and left[0].split("/")[-1] in {"RJ-S254201.pdf", "RJ-S254202.pdf", "RJ-AB100003.pdf"}, left


def _upload_review(db, g, s, name):
    """(upload status, upload notes, path of the original file) for a whole-upload Needs Review case."""
    up = db.q1("SELECT * FROM uploads WHERE file_name=%s", (name,))
    f = [f for f in g.files.values() if f["name"] == name][0]
    return up["status"], up["notes"] or "", g.path_of(f["id"])


@e2e
def test_mixed_upload_goes_whole_to_needs_review(env):
    """B-01: digital and scanned pages in one upload → whole file to Needs Review (E105), nothing split or stocked."""
    from estamp.cycle import run_cycle
    from fake_apis import FakeSplitter
    s, db, g = env
    g.add("mixed.pdf", PDF, s.folder("upload_inbox"), samples.merged([
        samples.paper_page(samples.cert("MH", 1), 200),
        samples.scanned_front("RAJASTHAN", "S 254201", 195), samples.scanned_back("RAJASTHAN")]))
    splitter = FakeSplitter()
    assert run_cycle(Ctx(s, db, g, splitter=splitter))
    status, notes, path = _upload_review(db, g, s, "mixed.pdf")
    assert status == "REVIEW" and "E105" in notes and path.startswith("needs_review/"), (status, notes, path)
    assert splitter.calls == [] and db.q1("SELECT count(*) n FROM papers")["n"] == 0


@e2e
def test_traditional_odd_page_count_goes_whole_to_review_before_split(env):
    """B-02: the split API would happily return a 1-page last part, so the odd count is caught BEFORE calling it."""
    from estamp.cycle import run_cycle
    from fake_apis import FakeSplitter
    s, db, g = env
    g.add("odd.pdf", PDF, s.folder("upload_inbox"), samples.merged([
        samples.scanned_front("RAJASTHAN", "S 254201", 195), samples.scanned_back("RAJASTHAN"),
        samples.scanned_front("RAJASTHAN", "S 254202", 195)]))
    splitter = FakeSplitter()
    assert run_cycle(Ctx(s, db, g, splitter=splitter))
    status, notes, path = _upload_review(db, g, s, "odd.pdf")
    assert status == "REVIEW" and "E106" in notes and path.startswith("needs_review/"), (status, notes, path)
    assert splitter.calls == [] and db.q1("SELECT count(*) n FROM papers")["n"] == 0


@e2e
def test_traditional_upload_is_split_in_pairs_and_each_page_ocrd(env):
    from estamp.cycle import run_cycle
    from fake_apis import FakeOcr, FakeSplitter
    s, db, g = env
    pages = []
    for serial in ["S 254201", "S 254202"]:
        pages += [samples.scanned_front("RAJASTHAN", serial, 195), samples.scanned_back("RAJASTHAN")]
    g.add("pairs.pdf", PDF, s.folder("upload_inbox"), samples.merged(pages))
    ocr, splitter = FakeOcr(), FakeSplitter()
    assert run_cycle(Ctx(s, db, g, ocr=ocr, splitter=splitter))
    assert splitter.calls == [("pairs.pdf", 2)]
    assert sorted(ocr.calls) == ["pairs.pdf_p1", "pairs.pdf_p2", "pairs.pdf_p3", "pairs.pdf_p4"]
    rows = db.q("SELECT cert_no, page_from, page_to, status FROM papers ORDER BY page_from")
    assert [(r["cert_no"], r["page_from"], r["page_to"], r["status"]) for r in rows] == [
        ("RJ-S254201", 1, 2, "AVAILABLE"), ("RJ-S254202", 3, 4, "AVAILABLE")]


@e2e
def test_missing_back_page_sends_that_pair_and_all_later_pairs_to_review(env):
    """B-03: F1 B1 F2 F3 B3 F4 – F2's back is missing, so F3 lands on F2's back side. Pair 1 is fine and is stocked;
    from page 3 on every pair is suspect (E107), even (B3, F4) which a naive check might pass."""
    from estamp.cycle import run_cycle
    s, db, g = env
    f = lambda n: samples.scanned_front("RAJASTHAN", f"S 25420{n}", 195)
    b = lambda: samples.scanned_back("RAJASTHAN")
    g.add("broken.pdf", PDF, s.folder("upload_inbox"), samples.merged([f(1), b(), f(2), f(3), b(), f(4)]))
    assert run_cycle(_ctx(s, db, g))
    rows = db.q("SELECT page_from, page_to, status, notes FROM papers ORDER BY page_from")
    assert [(r["page_from"], r["page_to"], r["status"]) for r in rows] == [
        (1, 2, "AVAILABLE"), (3, 4, "NEEDS_REVIEW"), (5, 6, "NEEDS_REVIEW")], rows
    assert all("E107" in r["notes"] and "page 3" in r["notes"] for r in rows[1:]), [r["notes"] for r in rows]


@e2e
def test_serial_the_api_returns_for_a_back_page_breaks_the_pairing(env):
    """The OCR API may return an ID directly (not in its text). One found on a back page means fronts have shifted."""
    from estamp.cycle import run_cycle
    from fake_apis import FakeOcr
    s, db, g = env
    pages = []
    for n in (1, 2):
        pages += [samples.scanned_front("RAJASTHAN", f"S 25420{n}", 195), samples.scanned_back("RAJASTHAN")]
    g.add("api_cert.pdf", PDF, s.folder("upload_inbox"), samples.merged(pages))
    ocr = FakeOcr(certs={"api_cert.pdf_p4": samples.cert("RJ", 9)})
    assert run_cycle(Ctx(s, db, g, ocr=ocr))
    rows = db.q("SELECT page_from, status, notes FROM papers ORDER BY page_from")
    assert [(r["page_from"], r["status"]) for r in rows] == [(1, "AVAILABLE"), (3, "NEEDS_REVIEW")], rows
    assert "E107" in rows[1]["notes"]


@e2e
def test_traditional_parts_must_be_two_pages(env):
    """A split that ignores splitSize (4-page parts) must not stock paper 2's front inside paper 1."""
    from estamp.cycle import run_cycle
    from fake_apis import FakeSplitter
    s, db, g = env
    pages = []
    for n in (1, 2):
        pages += [samples.scanned_front("RAJASTHAN", f"S 25420{n}", 195), samples.scanned_back("RAJASTHAN")]
    g.add("bad_split.pdf", PDF, s.folder("upload_inbox"), samples.merged(pages))
    ctx = Ctx(s, db, g, splitter=FakeSplitter(force_size=4))
    run_cycle(ctx)
    assert db.q1("SELECT count(*) n FROM papers")["n"] == 0
    assert any(f["name"] == "bad_split.pdf" for f in g.list_children(s.folder("upload_inbox")))   # retried later
    assert any("not 2 page(s) each" in a for a in ctx.alerts), ctx.alerts


@e2e
def test_digital_page_with_two_ids_goes_alone_to_review(env):
    from estamp.cycle import run_cycle
    s, db, g = env
    two = f"{samples.cert('MH', 1)} {samples.cert('MH', 2)}"
    g.add("two_ids.pdf", PDF, s.folder("upload_inbox"), samples.merged([
        samples.paper_page(two, 200), samples.paper_page(samples.cert("MH", 3), 200)]))
    assert run_cycle(_ctx(s, db, g))
    rows = db.q("SELECT page_from, status, notes FROM papers ORDER BY page_from")
    assert [(r["page_from"], r["status"]) for r in rows] == [(1, "NEEDS_REVIEW"), (2, "AVAILABLE")]
    assert "more than one" in rows[0]["notes"]


@e2e
def test_crash_mid_upload_then_rerun_stocks_nothing_twice(env, monkeypatch):
    """Crash while stocking pair 2 of 3: pair 1 is in the ledger, the file stays in the inbox. The next cycle finishes
    the file without stocking pair 1 again."""
    import estamp.intake as intake
    from estamp.cycle import run_cycle
    s, db, g = env
    pages = []
    for n in (1, 2, 3):
        pages += [samples.scanned_front("RAJASTHAN", f"S 25420{n}", 195), samples.scanned_back("RAJASTHAN")]
    g.add("crash.pdf", PDF, s.folder("upload_inbox"), samples.merged(pages))
    real = intake._stock_unit

    def crashing(ctx, up, name, u):
        if u.page_from == 3:
            raise RuntimeError("simulated crash")
        return real(ctx, up, name, u)

    monkeypatch.setattr(intake, "_stock_unit", crashing)
    run_cycle(_ctx(s, db, g))
    assert db.q1("SELECT count(*) n FROM papers WHERE page_from=3")["n"] == 0
    assert any(f["name"] == "crash.pdf" for f in g.list_children(s.folder("upload_inbox")))
    monkeypatch.setattr(intake, "_stock_unit", real)
    assert run_cycle(_ctx(s, db, g))
    rows = db.q("SELECT cert_no, page_from FROM papers ORDER BY page_from")
    assert [(r["cert_no"], r["page_from"]) for r in rows] == [("RJ-S254201", 1), ("RJ-S254202", 3), ("RJ-S254203", 5)]
    assert not g.list_children(s.folder("upload_inbox"))


@e2e
def test_estamps_never_grouped(env):
    """E-stamps are one page each. One with an unreadable certificate must not swallow the next page as its 'back'."""
    from estamp.cycle import run_cycle
    s, db, g = env
    g.add("ka_estamps.pdf", PDF, s.folder("upload_inbox"), samples.merged([samples.estamp_no_cert() for _ in range(4)]))
    assert run_cycle(_ctx(s, db, g))
    rows = db.q("SELECT page_from, page_to, status FROM papers ORDER BY page_from")
    assert [(r["page_from"], r["page_to"]) for r in rows] == [(1, 1), (2, 2), (3, 3), (4, 4)], rows
    assert all(r["status"] == "NEEDS_REVIEW" for r in rows)


def test_digital_pdf_skips_ocr(settings):
    """A digital e-stamp is read from its built-in text; OCR (google) is never called."""
    from estamp.ocr_split import read_paper

    class NoOCR:
        def ocr_text(self, *a):
            raise AssertionError("OCR must not be called for a digital PDF")

    r = read_paper(samples.paper_page(samples.cert("MH", 1), 200), "x", settings, NoOCR())
    assert r.via == "pdf-text" and samples.cert("MH", 1) in r.text


UP_P2 = """INDIA NON JUDICIAL Government of Uttar Pradesh\ne-Stamp\n100 100 100 100\n\\*0000000100\nCertificate No.\nCertificate Issued Date Account Reference.\nUnique Doc. Reference\nPurchased by\nStamp Duty Paid By\n23-Sep-2026 06:38 PM\nNEWIMPACC (SV)/ up14054804/ GAUTAMBUDDH NAGAR 2/ UP-GBN\n:SUBIN-UPUP1405480472263158537115Y\nSHRIRAM FINANCE LIMITED\nStamp Duty Amount(Rs.)\n100\n(One Hundred only)\n"""
UP_P3 = """INDIA NON JUDICIAL Government of Uttar Pradesh\ne-Stamp\nCertificate No.\nStamp Duty Paid By\nStamp Duty Amount (Rs.)\n23-Sep-2026 06:39 PM\nNEWIMPACC (SV)/ up14054804/ GAUTAMBUDDH NAGAR 2/ UP-GBN\n:SUBIN-UPUP1405480472263355590018Y\nSHRIRAM FINANCE LIMITED\n100\n(One Hundred only)\n"""


KA_P1 = """INDIA NON JUDICIAL\nGovernment of Karnataka\ne-Stamp\nSHRIRAM FINANCE LIMITED SHRIRAM FINANCE\nSHRI Certificate No. RIRAM FINANCE LIMITED SHRIRAM FIN\nEDISH Unique Doc. Reference NCEL\n: 23-Sep-2026 08:53 PM : NONACC (FI)/ kakscsa08/ KORAMANGALA17/ KA-JYTED SHRIRAM FINANCE\n: SUBIN-KAKAKSCSA0817022928679844Y SHRIRAM FINANCE LIMITED SHRIRAM FINAN\nArticle 5(J) Agreement (in any other cases)\nDEED OF HYPOTHECATION\n: 0\n(Zero)\nConsideration Price (Rs.)\nStamp Duty Paid By FINANCE LIMITED SHRIRAM: ANGEL Stamp Duty Amount (Rs.) RAM FINANCE LIMITE: 500 FINANCE LIMITED SHRIRAM\n"""


@pytest.mark.parametrize("text,ref,state,amount", [
    (UP_P2, "SUBIN-UPUP1405480472263158537115Y", "UP", 100),
    (UP_P3, "SUBIN-UPUP1405480472263355590018Y", "UP", 100),
    (KA_P1, "SUBIN-KAKAKSCSA0817022928679844Y", "KA", 500),   # office code in the ref; watermark inside the amount line
])
def test_real_estamp_text(settings, text, ref, state, amount):
    """Real e-stamp OCR text: certificate no. not readable → Unique Doc. Reference used."""
    r = StateResolver(settings.states); m = CertMatcher(settings.get("intake.cert_patterns"), r)
    a = analyze_paper(text, "", settings, m, lambda c: False)
    assert (a.cert, a.state, a.amount, a.issues) == (ref, state, amount, [])


@pytest.mark.parametrize("words,n", [("One Hundred", 100), ("One Hundred Ninety Five", 195), ("One Thousand", 1000),
                                     ("Three Hundred Fifty", 350), ("Fifty", 50), ("One Lakh Twenty Thousand", 120000)])
def test_words_to_number(words, n):
    from estamp.extract import words_to_number
    assert words_to_number(words) == n


@pytest.mark.parametrize("variant", [
    "SUBIN-UPUP1405480472259151020965Y\nStamp Duty Amount (Rs.)\n23-Sep-2026\nSHRIRAM\n(One Hundred only)\nINDIA NON JUDICIAL",
    "\\*0000000100\nSUBIN-UPUP1405480472259151020965Y\nStamp Duty Amount (Rs.)\nSHRIRAM FINANCE LIMITED",
])
def test_up_estamp_amount_layout_variants(settings, variant):
    """Amount found even when the number isn't next to its label (words, or the *0000000100 code)."""
    r = StateResolver(settings.states); m = CertMatcher(settings.get("intake.cert_patterns"), r)
    a = analyze_paper(variant, "", settings, m, lambda c: False)
    assert (a.state, a.amount, a.issues) == ("UP", 100, [])
