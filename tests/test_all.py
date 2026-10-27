"""Run: ESTAMP_TEST_DB_URL=postgresql://… python -m pytest -q tests/
The end-to-end tests create and drop their own schema (estamp_test); they never touch anything else.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

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
    """Merged scan: physical papers (front+back) mixed with an e-stamp – grouped, routed, issued, verified."""
    from estamp.cycle import run_cycle
    s, db, g = env
    pages = []
    for i, serial in enumerate(["S 254201", "S 254202", "AB 100003"]):
        pages += [samples.physical_front("RAJASTHAN", serial, 195), samples.physical_back("RAJASTHAN")]
    pages += [samples.physical_front("RAJASTHAN", "(serial smudged)", 195), samples.physical_back("RAJASTHAN")]
    pages.append(samples.paper_page(samples.cert("MH", 1), 200))
    g.add("vendor_scan.pdf", PDF, s.folder("upload_inbox"), samples.merged(pages))
    g.add("orders", GSHEET, s.folder("orders_incoming"),
          tabs={"x": [["LOAN NO", "STATE"], ["RJLAN0001", "RAJASTHAN"], ["RJLAN0002", "RAJASTHAN"]]})
    ctx = _ctx(s, db, g)
    assert run_cycle(ctx)
    smudged = db.q("SELECT * FROM papers WHERE cert_no IS NULL")
    assert len(smudged) == 1 and smudged[0]["page_to"] - smudged[0]["page_from"] == 1, smudged   # front+back kept together
    assert smudged[0]["status"] == "NEEDS_REVIEW"
    papers = {r["cert_no"]: r for r in db.q("SELECT * FROM papers WHERE cert_no IS NOT NULL")}
    assert set(papers) == {"RJ-S254201", "RJ-S254202", "RJ-AB100003", samples.cert("MH", 1)}, set(papers)
    assert all(papers[k]["page_to"] - papers[k]["page_from"] == 1 for k in papers if k.startswith("RJ-"))
    done = db.q("SELECT o.lan, a.cert_no, a.verified_cert, a.pdf_file_id FROM orders o JOIN allocations a ON a.order_id=o.id")
    assert len(done) == 2 and all(d["verified_cert"] == d["cert_no"] for d in done), done
    from pypdf import PdfReader
    import io as _io
    assert all(len(PdfReader(_io.BytesIO(g.files[d["pdf_file_id"]]["data"])).pages) == 2 for d in done)
    left = [g.path_of(f["id"]) for f in g.files.values() if "inventory_root/RJ/195/" in g.path_of(f["id"])]
    assert len(left) == 1 and left[0].split("/")[-1] in {"RJ-S254201.pdf", "RJ-S254202.pdf", "RJ-AB100003.pdf"}, left


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
