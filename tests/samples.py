"""Sample Shriram order sheets (amounts per the daily projection) and clearly-marked dummy papers for tests."""
from __future__ import annotations

import io

from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

H = ["Sr No", "LAN ID", "Customer Name", "Branch", "State", "Stamp Duty Amount"]


def mh():
    rows = [H]
    branches = ["Pune", "Pune", "Mumbai - Andheri", "Mumbai - Andheri", "Nashik", "Nashik", "Nagpur", "Nagpur",
                "Pune", "Thane", "Thane", "Kolhapur"]
    for i, b in enumerate(branches, 1):
        rows.append([str(i), f"TEST-MH-{i:06d}", f"Test Customer MH{i:02d}", b, "MH", "200"])
    rows.append(["13", "TEST-MH-000003", "Test Customer MH03", "Mumbai - Andheri", "MH", "200"])   # dup in file
    rows.append(["14", "", "Test Customer MH14", "Mumbai - Andheri", "MH", "200"])                 # blank LAN
    return rows


def ka():
    rows = [H]
    for i, b in enumerate(["Bengaluru - Jayanagar"] * 3 + ["Mysuru"] * 3 + ["Hubballi"] * 2 + ["Mangaluru", "Belagavi"], 1):
        rows.append([str(i), f"TEST-KA-{i:06d}", f"Test Customer KA{i:02d}", b, "KARTNATAKA", "Rs. 500"])
    return rows


def tn():  # no State column – state must come from the file name
    rows = [["Sr No", "LAN ID", "Customer Name", "Branch", "Stamp Duty Amount"]]
    for i, b in enumerate(["Chennai - T Nagar"] * 3 + ["Coimbatore"] * 2 + ["Madurai"] * 2 + ["Trichy", "Salem", "Salem"], 1):
        rows.append([str(i), f"TEST-TN-{i:06d}", f"Test Customer TN{i:02d}", b, "200"])
    return rows


def gj():
    rows = [H]
    for i, b in enumerate(["Ahmedabad"] * 2 + ["Surat"] * 2 + ["Vadodara"] * 2 + ["Rajkot"] * 2 + ["Bhavnagar"] * 2, 1):
        rows.append([str(i), f"TEST-GJ-{i:06d}", f"Test Customer GJ{i:02d}", b, "gujarat", "300"])
    rows.append(["11", "TEST-GJ-000011", "Test Customer GJ11", "Surat", "gujarat", "250"])          # not GJ's amount
    rows.append(["12", "TEST-MH-000005", "Test Customer MH05", "Nashik", "gujarat", "300"])         # MH LAN in GJ sheet
    return rows


def up():  # title rows above the header
    rows = [["Shriram Finance - Uttar Pradesh LAN list", "", "", "", "", ""], ["Date: 26-Oct-2026"], ["", "", ""], H]
    for i, b in enumerate(["Lucknow"] * 2 + ["Kanpur"] * 2 + ["Agra"] * 2 + ["Varanasi"] * 2 + ["Meerut", "Prayagraj"], 1):
        rows.append([str(i), f"TEST-UP-{i:06d}", f"Test Customer UP{i:02d}", b, "UTTARPRADESH", "100"])
    return rows


SHEETS = {
    "TEST_Shriram_MH_LANs_26Oct": mh,
    "TEST_Shriram_Karnataka_LANs_26Oct": ka,
    "TEST_Shriram_TN_LANs_26Oct": tn,
    "TEST_Shriram_GJ_LANs_26Oct": gj,
    "TEST_Shriram_UP_LANs_26Oct": up,
}


def to_csv(rows) -> str:
    import csv
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue()


# ------------------------------------------------------------------ dummy papers

def paper_page(cert: str, amount: int, party="SHRIRAM FINANCE LIMITED") -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(50, 780, "TEST DOCUMENT - NOT A STAMP PAPER")
    c.setFont("Helvetica", 12)
    y = 740
    for line in [f"Certificate No. : {cert}" if cert else "Certificate No. : (unreadable)",
                 "Certificate Issued Date : 26-Oct-2026", f"First Party : {party}",
                 f"Stamp Duty Amount(Rs.) : {amount}"]:
        c.drawString(50, y, line)
        y -= 24
    c.save()
    return buf.getvalue()


def merged(pages: list[bytes]) -> bytes:
    w = PdfWriter()
    for p in pages:
        w.add_page(PdfReader(io.BytesIO(p)).pages[0])
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def cert(state: str, n: int, letter="A") -> str:
    return f"IN-{state}{90000000000000 + n:014d}{letter}"


def physical_front(state_name: str, serial: str, amount: int) -> bytes:
    buf = io.BytesIO(); c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica-Bold", 16); c.drawString(50, 780, "TEST DOCUMENT - NOT A STAMP PAPER")
    c.setFont("Helvetica", 12); y = 740
    for line in ["INDIA NON JUDICIAL", "ONE HUNDRED NINETY FIVE RUPEES", f"Rs.{amount}", state_name, "24 SEP 2026", serial]:
        c.drawString(50, y, line); y -= 24
    c.save(); return buf.getvalue()


def physical_back(state_name: str, buyer="SHRIRAM FINANCE LIMITED") -> bytes:
    buf = io.BytesIO(); c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica", 12); y = 780
    for line in ["245117", "24.09.2026", buyer, state_name, "Licence No. 28 / 2024", "Vendor signature"]:
        c.drawString(50, y, line); y -= 24
    c.save(); return buf.getvalue()


def estamp_no_cert(state_name: str = "Karnataka") -> bytes:
    """E-stamp page whose certificate isn't readable – it still prints INDIA NON JUDICIAL like physical paper."""
    buf = io.BytesIO(); c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica-Bold", 16); c.drawString(50, 780, "TEST DOCUMENT - NOT A STAMP PAPER")
    c.setFont("Helvetica", 12); y = 740
    for line in ["INDIA NON JUDICIAL", f"Government of {state_name}", "e-Stamp", "Certificate No. : (unreadable)"]:
        c.drawString(50, y, line); y -= 24
    c.save(); return buf.getvalue()


# ------------------------------------------------------------------ scanned (image-only) pages
# A real scan has no text layer. These pages are a blank image plus a hidden page key holding what a perfect OCR
# would read, so FakeOcr / FakeGoogle.ocr_text can "read" them while paper-type detection sees no text at all.
OCR_KEY = "/EstampTestOcr"


def scanned_page(lines: list[str]) -> bytes:
    from PIL import Image
    from pypdf.generic import NameObject, TextStringObject
    from reportlab.lib.utils import ImageReader
    buf = io.BytesIO(); c = canvas.Canvas(buf, pagesize=A4)
    c.drawImage(ImageReader(Image.new("L", (60, 40), 200)), 50, 500, 400, 300); c.save()
    w = PdfWriter(); w.add_page(PdfReader(io.BytesIO(buf.getvalue())).pages[0])
    w.pages[0][NameObject(OCR_KEY)] = TextStringObject("\n".join(lines))
    out = io.BytesIO(); w.write(out); return out.getvalue()


def scanned_front(state_name: str, serial: str, amount: int) -> bytes:
    return scanned_page(["INDIA NON JUDICIAL", f"Rs.{amount}", state_name, "24 SEP 2026", serial])


def scanned_back(state_name: str, buyer="SHRIRAM FINANCE LIMITED") -> bytes:
    return scanned_page(["245117", "24.09.2026", buyer, state_name, "Licence No. 28 / 2024"])


def ocr_text_of(data: bytes) -> str:
    """What a perfect OCR reads: the hidden key on scanned pages, the text layer otherwise."""
    out = []
    for p in PdfReader(io.BytesIO(data)).pages:
        out.append(str(p[OCR_KEY]) if OCR_KEY in p else (p.extract_text() or ""))
    return "\n".join(out)
