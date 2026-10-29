"""Pure logic (no Google, no DB): state resolution, OCR parsing, paper checks, order-sheet parsing.

Everything here is unit-tested in tests/test_all.py (the tests that take no DB / Drive fixture).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .settings import Settings, StateCfg


# ============================== STATES ==============================

class StateResolver:
    """Maps any spelling Shriram or a certificate uses ("Kartnataka", "Delhi NCR", "IN-TG…") to a state code."""

    def __init__(self, states: dict[str, StateCfg]):
        self.states = states
        self.lookup: dict[str, str] = {}
        self.cert_lookup: dict[str, str] = {}
        for code, st in states.items():
            for key in [code, st.name, *st.aliases]:
                self.lookup[self._norm(key)] = code
            for cc in st.cert_codes:
                self.cert_lookup[cc.upper()] = code

    @staticmethod
    def _norm(v: str) -> str:
        s = str(v or "").upper().replace("&", " AND ")
        s = re.sub(r"[^A-Z ]+", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    def resolve(self, value: str) -> str:
        s = self._norm(value)
        if not s:
            return ""
        if s in self.lookup:
            return self.lookup[s]
        first = s.split(" ")[0]
        if len(first) == 2 and first in self.lookup:
            return self.lookup[first]
        return ""

    def from_cert_code(self, cc: str) -> str:
        return self.cert_lookup.get((cc or "").upper(), "")

    def from_file_name(self, name: str) -> str:
        """Full state names anywhere, or UPPERCASE 2-letter codes as separate tokens. Ambiguous → ''."""
        found: set[str] = set()
        padded = " " + self._norm(name) + " "
        for key, code in self.lookup.items():
            if len(key) > 3 and f" {key} " in padded:
                found.add(code)
        for tok in re.split(r"[^A-Za-z]+", str(name)):
            if len(tok) == 2 and tok.isupper() and tok in self.lookup:
                found.add(self.lookup[tok])
        return found.pop() if len(found) == 1 else ""


# ============================== OCR TEXT ==============================

MAX_OCR_TEXT = 20000    # chars of OCR text we parse and store per paper (L-4: bounds the regex passes below)


def normalize_ocr(text: str) -> str:
    t = str(text or "")[:MAX_OCR_TEXT].upper()
    t = re.sub(r"(\d)[ \t]+(?=\d)", r"\1", t)              # "1234 5678" → "12345678"
    t = re.sub(r"\bI\s*N\s*-\s*", "IN-", t)                 # "I N -" → "IN-"
    t = re.sub(r"\bIN-([A-Z])\s+([A-Z])", r"IN-\1\2", t)     # "IN-M H" → "IN-MH"
    t = re.sub(r"\bIN-([A-Z]{2})\s+(?=\d)", r"IN-\1", t)     # "IN-MH 1234" → "IN-MH1234"
    t = re.sub(r"(\bIN-[A-Z]{2}\d{10,16})[ \t]+([A-Z])\b", r"\1\2", t)  # e-stamp only: "IN-MH…1234 X" → "…1234X"
    return t


@dataclass
class CertHit:
    cert: str
    state: str          # resolved state code ('' if unknown)
    raw_code: str       # what the certificate itself said (e.g. TG)


class CertMatcher:
    """Certificate patterns from config: DEFAULT plus optional per-state overrides.

    DEFAULT's first group must be the 2-letter state code. A per-state pattern (key = state code)
    tells us the state directly, so it needs no state group.
    """

    def __init__(self, patterns: dict[str, str], resolver: StateResolver):
        self.resolver = resolver
        self.patterns = {k.upper(): re.compile(v) for k, v in patterns.items()}
        if "DEFAULT" not in self.patterns:
            raise ValueError("intake.cert_patterns must contain DEFAULT")

    def find(self, text: str) -> list[CertHit]:
        hits: dict[str, CertHit] = {}
        for key, rx in self.patterns.items():
            for m in rx.finditer(text):
                cert = m.group(0)
                if cert in hits:
                    continue
                if key == "DEFAULT":
                    raw = m.group(1) if m.groups() else ""
                    hits[cert] = CertHit(cert, self.resolver.from_cert_code(raw), raw)
                else:
                    hits[cert] = CertHit(cert, key, key)
        return list(hits.values())

    def full_match(self, cert: str, state: str) -> bool:
        rx = self.patterns.get(state.upper()) or self.patterns["DEFAULT"]
        return bool(cert) and rx.fullmatch(cert) is not None


def extract_amount(text: str, pattern) -> int:
    """First amount found. `pattern` may be one regex or a list tried in order (group 1 = the number)."""
    for pat in ([pattern] if isinstance(pattern, str) else list(pattern or [])):
        m = re.search(pat, text)
        if m:
            try:
                n = int(m.group(1).replace(",", ""))
            except ValueError:
                continue
            if n:
                return n
    return 0


_UNITS = {w: i for i, w in enumerate("ZERO ONE TWO THREE FOUR FIVE SIX SEVEN EIGHT NINE TEN ELEVEN TWELVE THIRTEEN "
                                     "FOURTEEN FIFTEEN SIXTEEN SEVENTEEN EIGHTEEN NINETEEN".split())}
_TENS = {w: 10 * (i + 2) for i, w in enumerate("TWENTY THIRTY FORTY FIFTY SIXTY SEVENTY EIGHTY NINETY".split())}


def words_to_number(words: str) -> int:
    """'One Hundred Ninety Five' → 195, 'One Thousand' → 1000, 'One Lakh Twenty Thousand' → 120000. 0 if not a number."""
    total = cur = 0
    seen = False
    for t in re.findall(r"[A-Z]+", words.upper()):
        if t in _UNITS:
            cur += _UNITS[t]; seen = True
        elif t in _TENS:
            cur += _TENS[t]; seen = True
        elif t == "HUNDRED":
            cur = (cur or 1) * 100; seen = True
        elif t == "THOUSAND":
            total += (cur or 1) * 1000; cur = 0; seen = True
        elif t in ("LAKH", "LAKHS", "LAC", "LACS"):
            total += (cur or 1) * 100000; cur = 0; seen = True
        elif t in ("AND", "RUPEES", "RUPEE", "RS", "ONLY"):
            continue
        else:
            return 0
    return total + cur if seen else 0


def amount_fallbacks(text: str) -> int:
    """Layout-independent amount reading for e-stamps: '(One Hundred only)' in words, then the '*0000000100' code."""
    m = re.search(r"\(\s*([A-Z][A-Z ]{2,80}?)\s+ONLY\s*\)", text)
    if m:
        n = words_to_number(m.group(1))
        if n:
            return n
    m = re.search(r"\*\s*0{4,}(\d{2,7})\b", text)
    return int(m.group(1)) if m else 0


@dataclass
class IdHit:
    cert: str           # e-stamp cert (IN-MH…) or physical id (RJ-S254201)
    state: str
    raw_code: str
    kind: str           # 'estamp' | 'physical'


def states_named_in(text: str, resolver: StateResolver) -> set[str]:
    """States whose full name is printed on the page (RAJASTHAN, TAMIL NADU …)."""
    padded = " " + re.sub(r"\s+", " ", re.sub(r"[^A-Z ]+", " ", str(text).upper())) + " "
    return {code for key, code in resolver.lookup.items() if len(key) > 3 and f" {key} " in padded}


def identify(text: str, settings: Settings, matcher: CertMatcher, resolver: StateResolver) -> list[IdHit]:
    """Unique id(s) on a page/paper: e-stamp certificate numbers first; otherwise a physical stamp paper's
    serial number, using the serial pattern of the state whose name is printed on it."""
    hits = [IdHit(h.cert, h.state, h.raw_code, "estamp") for h in matcher.find(text)]
    if hits:
        return hits
    # Some e-stamp PDFs don't expose the certificate no. as text; the Unique Doc. Reference
    # (SUBIN-UPUP…) is printed as text, is unique per certificate and carries the state code.
    refs: dict[str, IdHit] = {}
    for pat in settings.get("intake.estamp_ref_patterns") or []:
        for m in re.finditer(pat, text):
            raw = m.group(1) if m.groups() else ""
            refs.setdefault(m.group(0), IdHit(m.group(0), resolver.from_cert_code(raw), raw, "estamp"))
    if refs:
        return list(refs.values())
    patterns = settings.get("intake.serial_patterns") or {}
    out: dict[str, IdHit] = {}
    for st in sorted(states_named_in(text, resolver)):
        pat = patterns.get(st) or patterns.get("DEFAULT")
        if not pat:
            continue
        for m in re.finditer(pat, text):
            serial = re.sub(r"[^A-Z0-9]", "", m.group(0))
            cid = f"{st}-{serial}"
            out.setdefault(cid, IdHit(cid, st, st, "physical"))
    return list(out.values())


def id_format_ok(cert: str, state: str, settings: Settings, matcher: CertMatcher) -> bool:
    """Used when ops type a corrected id: e-stamp cert, or STATE-SERIAL for physical paper."""
    if matcher.full_match(cert, state):
        return True
    for pat in settings.get("intake.estamp_ref_patterns") or []:
        m = re.fullmatch(pat, cert)
        if m and (not m.groups() or matcher.resolver.from_cert_code(m.group(1)) == state):
            return True
    prefix = f"{state}-"
    if not cert.startswith(prefix):
        return False
    patterns = settings.get("intake.serial_patterns") or {}
    pat = patterns.get(state) or patterns.get("DEFAULT")
    return bool(pat) and re.fullmatch(pat, cert[len(prefix):]) is not None


@dataclass
class PaperAnalysis:
    cert: str = ""
    cert_state: str = ""
    state: str = ""
    amount: int = 0
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


def review_code(a: PaperAnalysis) -> str:
    """The error-catalogue code for a paper that failed its checks ('' = none fits, e.g. state inactive). Matches the
    issue wording written in this module; the strongest reason wins (a duplicate beats a bad amount)."""
    text = " ".join(a.issues)
    if "duplicate certificate" in text:
        return "E126"
    if "more than one certificate" in text:
        return "E121"
    if "is not in state_config.csv" in text:
        return "E122"
    if not a.cert:
        return "E120"
    if "amount not readable" in text:
        return "E124"
    if "not an allowed amount" in text:
        return "E125"
    return ""


def _check_state_and_amount(a: "PaperAnalysis", settings: Settings) -> None:
    st = settings.states.get(a.state) if a.state else None
    if st:
        if not st.active:
            a.issues.append(f"state {a.state} is marked inactive in state_config.csv ({st.notes or 'no note'})")
        if not a.amount:
            a.issues.append("stamp duty amount not readable")
        elif a.amount not in st.amounts:
            a.issues.append(f"stamp duty ₹{a.amount} is not an allowed amount for {a.state} {st.amounts}")


def analyze_paper(text: str, api_cert: str, settings: Settings, matcher: CertMatcher,
                  is_duplicate_cert, resolver: StateResolver | None = None) -> PaperAnalysis:
    """Decide where a paper belongs (state + denomination) and whether it's safe to stock.

    is_duplicate_cert: callable(cert) -> bool, checks the DB for a live paper with the same id.
    """
    a = PaperAnalysis()
    resolver = resolver or matcher.resolver
    t = normalize_ocr(text)
    hits = {h.cert: h for h in identify(t, settings, matcher, resolver)}
    if api_cert:
        api_hits = identify(normalize_ocr(api_cert), settings, matcher, resolver)
        if len(api_hits) == 1:
            hits.setdefault(api_hits[0].cert, api_hits[0])
        else:
            a.issues.append(f"OCR API returned cert '{api_cert}' that matches no known pattern")

    if not hits:
        a.issues.append("no certificate / serial number found")
    elif len(hits) > 1:
        a.issues.append("more than one certificate / serial number on this paper: " + ", ".join(hits))
    else:
        h = next(iter(hits.values()))
        a.cert, a.cert_state, a.state = h.cert, h.raw_code, h.state
        if not a.state:
            a.issues.append(f"certificate state code '{h.raw_code}' is not in state_config.csv")

    a.amount = extract_amount(t, settings.get("intake.amount_patterns") or settings.get("intake.stamp_duty_regex"))
    if not a.amount or (a.state in settings.states and a.amount not in settings.states[a.state].amounts):
        alt = amount_fallbacks(t)
        if alt:
            a.amount = alt
    _check_state_and_amount(a, settings)

    for needed in settings.get("intake.required_text", []) or []:
        if needed.upper() not in t:
            a.issues.append(f"required text '{needed}' not found on paper")

    if a.cert and is_duplicate_cert(a.cert):
        a.issues.append(f"duplicate certificate {a.cert} – already in stock or used")
    return a


# ============================== OCR API FIELDS (ADR-0006) ==============================
# The in-house OCR API returns structured fields only (no text): {"certification_no": {"value": …}, …}, `{}` when a
# field wasn't found. Contract: docs/api-samples/ocr/README.md.

_GENERIC_SERIAL = re.compile(r"(?=.*\d)[A-Z0-9]{5,16}")


def _field(fields: dict, name: str) -> str:
    return str(((fields or {}).get(name) or {}).get("value") or "").strip().upper()


def id_from_fields(fields: dict, settings: Settings, matcher: CertMatcher, resolver: StateResolver) -> IdHit | None:
    """The paper's ID from the API fields: an e-stamp certificate (IN-…) as is, else its Unique Doc. Reference
    (SUBIN-…), else a traditional serial as STATE-SERIAL (state from state_code / state_name). None = no ID."""
    cert = re.sub(r"\s+", "", _field(fields, "certification_no"))
    ref = re.sub(r"\s+", "", _field(fields, "unique_doc_reference"))
    state = resolver.resolve(_field(fields, "state_code")) or resolver.resolve(_field(fields, "state_name"))
    if cert.startswith("IN-"):
        hits = matcher.find(normalize_ocr(cert))
        h = hits[0] if len(hits) == 1 and hits[0].cert == cert else None
        return IdHit(cert, (h.state if h else "") or state, h.raw_code if h else "", "estamp")
    if ref.startswith("SUBIN-"):
        for pat in settings.get("intake.estamp_ref_patterns") or []:
            m = re.fullmatch(pat, ref)
            if m:
                raw = m.group(1) if m.groups() else ""
                return IdHit(ref, resolver.from_cert_code(raw) or state, raw, "estamp")
        return IdHit(ref, state, "", "estamp")
    if cert:
        serial = re.sub(r"[^A-Z0-9]", "", cert)
        return IdHit(f"{state}-{serial}" if state else serial, state, state, "physical")
    return None


def _id_looks_valid(h: IdHit, settings: Settings, matcher: CertMatcher) -> bool:
    if h.kind == "estamp":
        if h.cert.startswith("IN-"):
            return matcher.full_match(h.cert, h.state)
        return any(re.fullmatch(p, h.cert) for p in settings.get("intake.estamp_ref_patterns") or [])
    serial = h.cert.split("-", 1)[1] if "-" in h.cert else h.cert
    pat = (settings.get("intake.serial_patterns") or {}).get(h.state)     # per-state pattern if configured
    return re.fullmatch(pat, serial) is not None if pat else _GENERIC_SERIAL.fullmatch(serial) is not None


def analyze_fields(fields: dict, settings: Settings, matcher: CertMatcher, is_duplicate_cert,
                   resolver: StateResolver) -> PaperAnalysis:
    """analyze_paper for an OCR API reading: same checks (one sane ID, known active state, allowed amount, no
    duplicate). `intake.required_text` can't be checked – the API returns no text."""
    a = PaperAnalysis()
    h = id_from_fields(fields, settings, matcher, resolver)
    if not h:
        a.issues.append("no certificate / serial number found")
    else:
        a.cert, a.cert_state, a.state = h.cert, h.raw_code, h.state
        if not h.state:
            a.issues.append(f"state of '{h.cert}' is not in state_config.csv")
        elif not _id_looks_valid(h, settings, matcher):
            a.issues.append(f"'{h.cert}' does not look like a valid {h.state} paper ID")
    digits = re.sub(r"[^\d]", "", _field(fields, "stamp_duty_amount").split(".")[0])
    a.amount = int(digits) if digits else 0
    _check_state_and_amount(a, settings)
    if a.cert and is_duplicate_cert(a.cert):
        a.issues.append(f"duplicate certificate {a.cert} – already in stock or used")
    return a


def ids_in_reading(text: str, cert: str, fields: dict | None, settings: Settings, matcher: CertMatcher,
                   resolver: StateResolver) -> list[IdHit]:
    """IDs on one page, whichever reader produced it: the OCR API's fields, or a text reader's text (+ cert)."""
    if fields is not None:
        h = id_from_fields(fields, settings, matcher, resolver)
        return [h] if h else []
    return identify(normalize_ocr(text + ("\n" + cert if cert else "")), settings, matcher, resolver)


def pages_per_paper(h: IdHit, settings: Settings) -> int:
    """1 for an e-stamp; otherwise the state's pages_per_paper (state_config.csv, default 2 = front + back)."""
    if h.kind == "estamp":
        return 1
    st = settings.states.get(h.state)
    return st.pages_per_paper if st else 2


# ============================== PAIRING (ADR-0003) ==============================


def first_broken_pair(id_counts: list[tuple[int, int]]) -> int | None:
    """id_counts: (IDs found on the front page, IDs found on the back page) per traditional pair, in file order.
    A good pair has exactly one serial on the front and none on the back. Returns the index of the first bad pair
    – that pair and every later one go to Needs Review, since one missing page shifts every pair after it."""
    for i, (front, back) in enumerate(id_counts):
        if front != 1 or back != 0:
            return i
    return None


# ============================== ORDER SHEETS ==============================

def _norm_header(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def find_header(rows: list[list[str]], columns: dict[str, list[str]], scan_rows: int) -> tuple[int, dict[str, int]] | None:
    aliases = {k: {_norm_header(a) for a in v} for k, v in columns.items()}
    for r, row in enumerate(rows[:scan_rows]):
        cells = [_norm_header(c) for c in row]
        cols = {k: next((i for i, c in enumerate(cells) if c and c in al), -1) for k, al in aliases.items()}
        if cols.get("lan", -1) >= 0:
            return r, cols
    return None


def parse_amount(v: str) -> int:
    m = re.search(r"(\d+)(?:\.\d+)?", str(v or "").replace(",", ""))
    return int(m.group(1)) if m else 0


_ASCII_UPPER = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def canonical_lan(lan: str) -> str:
    """The one LAN form every match uses (ADR-0008): ALL whitespace removed (internal spaces, tabs, non-breaking
    spaces) + ASCII uppercase. Leading zeros stay significant. The raw LAN is kept separately for display.
    ASCII-only on purpose: str.upper() turns 'ﬁ' into 'FI' and 'ß' into 'SS', which could make pasted junk
    equal a real LAN."""
    return re.sub(r"\s+", "", str(lan or "")).translate(_ASCII_UPPER)


@dataclass
class OrderRow:
    lan: str
    state: str
    denomination: int
    tab: str
    row_no: int

    @property
    def lan_key(self) -> str:
        return canonical_lan(self.lan)


@dataclass
class ParsedOrderFile:
    valid: list[OrderRow] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    header_found: bool = False


def parse_order_tabs(tabs: Iterable[tuple[str, list[list[str]]]], file_name: str,
                     settings: Settings, resolver: StateResolver) -> ParsedOrderFile:
    out = ParsedOrderFile()
    columns = settings.get("orders.columns")
    scan = int(settings.get("orders.header_scan_rows", 10))
    lan_rx = re.compile(settings.get("orders.lan_regex"))
    file_state = resolver.from_file_name(file_name)
    seen: set[str] = set()

    for title, rows in tabs:
        hdr = find_header(rows, columns, scan)
        if not hdr:
            continue
        out.header_found = True
        hrow, cols = hdr
        tab_state = resolver.resolve(title)
        for r in range(hrow + 1, len(rows)):
            row = [str(c if c is not None else "").strip() for c in rows[r]]
            if not any(row):
                continue
            where = f"{title}!row {r + 1}"
            cell = lambda k: row[cols[k]] if 0 <= cols.get(k, -1) < len(row) else ""
            # Display form: whitespace runs (tabs, NBSP, 500 spaces) → one space – it names <LAN>.pdf and the
            # Output sheet row. Matching and the format check use the canonical key ("AB 123" is AB123).
            lan = re.sub(r"\s+", " ", cell("lan"))
            if not lan:
                out.invalid.append(f"{where}: LAN blank")
                continue
            if not lan_rx.match(canonical_lan(lan)):
                out.invalid.append(f"{where}: LAN '{lan}' fails format check")
                continue
            state = resolver.resolve(cell("state")) if cols.get("state", -1) >= 0 else ""
            if cols.get("state", -1) >= 0 and cell("state") and not state:
                out.invalid.append(f"{where}: unknown state '{cell('state')}'")
                continue
            state = state or tab_state or file_state
            if not state:
                out.invalid.append(f"{where}: state could not be determined")
                continue
            st = settings.states[state]
            if not st.active:
                out.invalid.append(f"{where}: state {state} is inactive ({st.notes or 'see state_config.csv'})")
                continue
            amt = parse_amount(cell("denomination")) if cols.get("denomination", -1) >= 0 else 0
            if not amt:
                if len(st.amounts) == 1:
                    amt = st.amounts[0]
                else:
                    out.invalid.append(f"{where}: stamp duty missing and {state} has several amounts")
                    continue
            if amt not in st.amounts:
                out.invalid.append(f"{where}: ₹{amt} is not an allowed stamp duty for {state} {st.amounts}")
                continue
            o = OrderRow(lan=lan, state=state, denomination=amt, tab=title, row_no=r + 1)
            if o.lan_key in seen:
                out.duplicates.append(f"{where}: {lan} repeated in the same file")
                continue
            seen.add(o.lan_key)
            out.valid.append(o)
    return out


def safe_file_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|#%\r\n]+', "_", str(s).strip())
