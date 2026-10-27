"""Central error catalogue – one place for the wording ops sees.

Every problem ops can hit has a code (E1xx intake, E2xx orders, E3xx issue, E4xx
sheets/reconcile, E5xx environment). Each code maps to three short phrases:
`when` it triggers, what the system `does`, and what `ops` must do. `line()` makes
the one-line terminal/Log message; `explain()` prints the full breakdown (used by
`python -m estamp explain <code>`). Pure module: no Google, no DB, no estamp imports –
keep it that way so the catalogue stays the single source of wording.

Call sites still emit codes their own way for now; rewiring them onto this catalogue
is a follow-up slice.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Err:
    when: str  # what triggers it
    does: str  # what the system does
    ops: str   # what the ops person must do


# Seeded from HANDOFF.md section 5. Plain data – edit wording here, nowhere else.
CATALOGUE: dict[str, Err] = {
    "E101": Err("File not inside a vendor folder", 'Leaves it, Needs Review "vendor unknown"', "Move it into the right vendor folder"),
    "E102": Err("Not a PDF", "Moves to Needs Review/Not PDF", "Re-export as PDF and upload"),
    "E103": Err("PDF corrupt / password-protected", "Needs Review", "Get a clean file from vendor"),
    "E104": Err("Exact duplicate upload", "Not stocked again", "Nothing"),
    "E105": Err("Paper type unclear / mixed file", "Whole file to Needs Review", "Split digital and scanned into separate files, upload again"),
    "E106": Err("Traditional file with odd page count", "Whole file to Needs Review", "Find the missing page, upload again"),
    "E107": Err("Pairing broken at page N", "That pair + later pairs to Needs Review", "Upload those papers as separate 2-page PDFs; REJECT broken entries"),
    "E110": Err("Split API failed", "Retries; then Needs Review", "Nothing; tell tech if repeating"),
    "E111": Err("OCR API failed / timeout", "Retries; then Needs Review + alert", "Nothing; tell tech if repeating 30 min"),
    "E112": Err("OCR rate-limited", "Slows down automatically (info)", "Nothing"),
    "E113": Err("OCR/split API key rejected", "Stops intake, alert", "Tell tech team immediately"),
    "E120": Err("No certificate / serial found (after auto second API attempt)", "Needs Review, shows API output", "APPROVE with the correct ID from the paper, or RE-OCR, or REJECT"),
    "E121": Err("More than one ID on a paper", "Needs Review", "Check file; re-upload correctly"),
    "E122": Err("State unknown", "Needs Review", "Tell tech (state spelling)"),
    "E124": Err("Stamp duty unreadable (after auto second API attempt)", "Needs Review, shows API output", "APPROVE with the correct amount, or RE-OCR, or REJECT"),
    "E125": Err("Stamp duty not allowed for state", "Needs Review, shows API output", "If config was wrong: tech fixes, ops RECHECK; if paper is wrong: REJECT"),
    "E126": Err("Duplicate certificate / serial", "Needs Review (never stocked twice)", "REJECT; raise with vendor"),
    "E201": Err("Order file unreadable / wrong type", "Moves to Orders Rejected", "Send as .xlsx / Google Sheet"),
    "E202": Err("No LOAN NO column", "Orders Rejected", "Fix the Excel, drop again"),
    "E203": Err("LAN blank / invalid in a row", "Row skipped", "Fix row, re-send"),
    "E204": Err("State unknown in a row", "Row skipped", "Fix spelling / tell tech"),
    "E205": Err("LAN repeated / already received", "Skipped", "Nothing"),
    "E206": Err("Same LAN for two states", "Both held (CONFLICT)", "Order Actions: KEEP_STATE"),
    "E207": Err("LAN already issued earlier", "Skipped", "Nothing (or CANCEL if wrong)"),
    "E301": Err("No paper for state + amount", "LAN waits, shortage alert", "Upload papers for that state"),
    "E302": Err("Issued PDF shows different ID", "Paper quarantined, LAN re-mapped (D7)", "Check quarantined paper"),
    "E303": Err("Mismatch again on same LAN", "LAN ERROR", "Order Actions: RETRY after checking"),
    "E304": Err("Output file name already exists", "LAN ERROR", "Tell tech"),
    "E305": Err("Drive move/copy failed", "Retries; then LAN ERROR", "Tell tech if repeating"),
    "E306": Err("Recovered an interrupted issue", "Fixed automatically (info)", "Nothing"),
    "E401": Err("Output sheet write failed", "Retries next cycle", "Nothing"),
    "E402": Err("Reconciliation totals don't balance", "Red row + alert", "Tell tech"),
    "E501": Err("Google sign-in expired", "Stops, alert", "Run `python -m estamp login`"),
    "E502": Err("Database not reachable", "Stops", "Open Postgres.app, run again"),
    "E503": Err("No internet", "Waits, resumes", "Check Wi-Fi"),
    "E504": Err("Another run already active", "Skips this run", "Nothing"),
    "E505": Err("Drive storage low", "Alert", "Tell tech"),
    "E506": Err("Config error (folder/ID missing)", "Stops with details", "Tell tech"),
}


def line(code: str, context: str = "") -> str:
    """One-line ops message: ✗ [E111] <short when> … <context>."""
    rec = CATALOGUE.get(code)
    short = rec.when if rec else "unknown error code"
    tail = f" … {context}" if context else ""
    return f"✗ [{code}] {short}{tail}"


def explain(code: str) -> str:
    """Full human breakdown for `explain <code>`; unknown codes return a clear note, no raise."""
    rec = CATALOGUE.get(code)
    if rec is None:
        known = ", ".join(sorted(CATALOGUE))
        return f"[{code}] unknown error code.\n   Known codes: {known}"
    return (
        f"[{code}] {rec.when}\n"
        f"   What I did:  {rec.does}\n"
        f"   What you do: {rec.ops}"
    )
