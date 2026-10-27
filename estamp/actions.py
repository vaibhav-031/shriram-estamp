"""Ops actions, entered as rows in two control-sheet tabs. The service only ever fills the Result columns,
so ops rows are never overwritten.

Review Actions:  Paper ID | Action (APPROVE / REJECT / RE-OCR) | Cert No | State | Stamp Duty | Edited By | Result | Processed At
   APPROVE – optional corrected Cert No / State / Stamp Duty; checks run again, then the paper moves to
             Inventory/<STATE>/<AMOUNT>/<CERT>.pdf and becomes AVAILABLE. A correction records an audit trail
             (the pre-edit API values + who/when) for the Corrections tab (E-07).
   REJECT  – paper is never used (stays in Needs Review for the vendor dispute).
   RE-OCR  – re-reads the paper from its stored PDF (a NEW OCR call, unlike recheck) and either moves it to
             Inventory if it now passes, or refreshes its Needs Review notes with the new reading.

Order Actions:   LAN ID | Action (CANCEL / RETRY / KEEP_STATE) | State | Result | Processed At
   CANCEL     – unfulfilled LAN → CANCELLED. Fulfilled LAN → paper goes back to stock (renamed to its cert),
                Output sheet gets a CANCELLED row.
   RETRY      – LAN in ERROR → PENDING (gets a fresh paper; the old paper stays ERROR for checking).
   KEEP_STATE – LAN in CONFLICT → the row for the given State becomes PENDING, the others CANCELLED.
"""
from __future__ import annotations

from .allocate import label_for
from .constants import INVENTORY_ROOT
from .context import Ctx
from .db import UniqueViolation
from .extract import id_format_ok, parse_amount
from .outputs import output_sheet_id

REVIEW_TAB, ORDER_TAB = "Review Actions", "Order Actions"
REVIEW_HEADER = ["Paper ID", "Action", "Cert No", "State", "Stamp Duty", "Edited By", "Result", "Processed At"]
ORDER_HEADER = ["LAN ID", "Action", "State", "Result", "Processed At"]


def run_actions(ctx: Ctx) -> None:
    sid = ctx.settings.get("control_sheet_id")
    if not sid or ctx.dry:
        return
    ctx.google.ensure_tabs(sid, {REVIEW_TAB: REVIEW_HEADER, ORDER_TAB: ORDER_HEADER})
    updates = []
    for i, row in enumerate(ctx.google.read_range(sid, f"'{REVIEW_TAB}'!A2:H"), start=2):
        row = (row + [""] * 8)[:8]
        if row[1].strip() and not row[6].strip():
            updates.append((f"'{REVIEW_TAB}'!G{i}:H{i}", [[_review(ctx, row), ctx.now_str()]]))
    for i, row in enumerate(ctx.google.read_range(sid, f"'{ORDER_TAB}'!A2:E"), start=2):
        row = (row + [""] * 5)[:5]
        if row[1].strip() and not row[3].strip():
            updates.append((f"'{ORDER_TAB}'!D{i}:E{i}", [[_order(ctx, row), ctx.now_str()]]))
    ctx.google.write_ranges(sid, updates)
    ctx.stats.actions += len(updates)


def _review(ctx: Ctx, row: list[str]) -> str:
    db, g, s = ctx.db, ctx.google, ctx.settings
    pid, action = row[0].strip(), row[1].strip().upper()
    if not pid.isdigit():
        return "ERROR: Paper ID must be a number (see Needs Review tab)"
    p = db.q1("SELECT * FROM papers WHERE id=%s", (int(pid),))
    if not p:
        return "ERROR: no such paper"
    if p["status"] != "NEEDS_REVIEW":
        return f"SKIPPED: paper is {p['status']}, not NEEDS_REVIEW"
    if action == "REJECT":
        db.update_paper(p["id"], status="REJECTED", notes=f"rejected by ops | was: {p['notes']}")
        ctx.event("INFO", "Review", f"paper {pid} rejected")
        return "DONE: rejected"
    if action == "RE-OCR":
        return _re_ocr(ctx, p)
    if action != "APPROVE":
        return "ERROR: Action must be APPROVE, REJECT or RE-OCR"

    cert = (row[2].strip().upper() or p["cert_no"] or "")
    state = ctx.resolver.resolve(row[3]) if row[3].strip() else (p["state"] or "")
    amount = parse_amount(row[4]) if row[4].strip() else (p["denomination"] or 0)
    st = s.states.get(state)
    if not st:
        return f"ERROR: unknown state '{row[3] or p['state']}'"
    if not st.active:
        return f"ERROR: state {state} is inactive"
    if amount not in st.amounts:
        return f"ERROR: ₹{amount} not allowed for {state} {st.amounts}"
    if not id_format_ok(cert, state, s, ctx.matcher):
        return f"ERROR: '{cert}' is not a valid certificate / serial for {state} (physical paper: {state}-<SERIAL>, e.g. {state}-S254201)"
    # E-07: a correction (cert/state/amount changed) records an audit trail of the pre-edit API values + who/when.
    audit: dict = {}
    if (cert, state, amount) != (p["cert_no"], p["state"], p["denomination"]):
        if p.get("api_cert_no") is None and p.get("api_state") is None and p.get("api_denomination") is None:
            audit.update(api_cert_no=p["cert_no"], api_state=p["state"], api_denomination=p["denomination"])
        audit.update(ops_edited_by=(row[5].strip() or "ops"), ops_edited_at=ctx.now())
    try:
        db.update_paper(p["id"], cert_no=cert, state=state, denomination=amount, status="AVAILABLE",
                        notes=f"approved by ops | was: {p['notes']}", **audit)
    except UniqueViolation:
        return f"ERROR: certificate {cert} is already in stock / used – this looks like a duplicate"
    try:
        g.move_rename(p["drive_file_id"], g.folder_path(s.folder(INVENTORY_ROOT), [state, str(amount)]), f"{cert}.pdf")
        db.update_paper(p["id"], file_name=f"{cert}.pdf")
    except Exception as e:  # paper is usable anyway; allocation moves it from wherever it is
        ctx.event("WARN", "Review", f"paper {pid} approved but could not be moved into Inventory: {e}")
    ctx.event("INFO", "Review", f"paper {pid} approved as {cert} {state} ₹{amount}")
    return f"DONE: approved → {state}/{amount}/{cert}.pdf"


def _re_ocr(ctx: Ctx, p: dict) -> str:
    """Re-read a NEEDS_REVIEW paper from its stored PDF (a fresh OCR call) and re-analyze. Passes → Inventory;
    otherwise its Needs Review notes are refreshed with the new reading. Mirrors recheck_review, but re-OCRs."""
    from .extract import analyze_fields, analyze_paper
    db, g, s = ctx.db, ctx.google, ctx.settings
    if not p["drive_file_id"]:
        return "ERROR: paper has no stored PDF to re-OCR"
    res = ctx.ocr.read(g.download(p["drive_file_id"]), p["file_name"] or f"paper_{p['id']}.pdf")
    if res.fields is not None:
        a = analyze_fields(res.fields, s, ctx.matcher, db.cert_is_live, ctx.resolver)
    else:
        a = analyze_paper(res.text, res.cert, s, ctx.matcher, db.cert_is_live, ctx.resolver)
    if not a.ok:
        db.update_paper(p["id"], ocr_raw=res.raw, ocr_via=res.via, ocr_text=res.text or None, notes="; ".join(a.issues))
        ctx.event("INFO", "Re-OCR", f"paper {p['id']} re-read, still needs review: {'; '.join(a.issues)}")
        return f"DONE: re-OCR'd, still needs review: {'; '.join(a.issues)}"
    try:
        db.update_paper(p["id"], cert_no=a.cert, cert_state=a.cert_state, state=a.state, denomination=a.amount,
                        ocr_amount=a.amount, ocr_raw=res.raw, ocr_via=res.via, ocr_text=res.text or None,
                        status="AVAILABLE", notes=f"re-OCR OK | was: {p['notes']}")
    except UniqueViolation:
        db.update_paper(p["id"], notes=f"duplicate certificate {a.cert} – already in stock")
        return f"ERROR: certificate {a.cert} is already in stock / used – this looks like a duplicate"
    try:
        g.move_rename(p["drive_file_id"], g.folder_path(s.folder(INVENTORY_ROOT), [a.state, str(a.amount)]), f"{a.cert}.pdf")
        db.update_paper(p["id"], file_name=f"{a.cert}.pdf")
    except Exception as e:
        ctx.event("WARN", "Re-OCR", f"paper {p['id']} re-OCR passed but could not be moved into Inventory: {e}")
    ctx.event("INFO", "Re-OCR", f"paper {p['id']} re-OCR'd and moved to Inventory as {a.cert} {a.state} ₹{a.amount}")
    return f"DONE: re-OCR'd → {a.state}/{a.amount}/{a.cert}.pdf"


def _order(ctx: Ctx, row: list[str]) -> str:
    db, g, s = ctx.db, ctx.google, ctx.settings
    lan_key, action = row[0].strip().upper(), row[1].strip().upper()
    rows = db.q("SELECT * FROM orders WHERE lan_key=%s AND status <> 'CANCELLED' ORDER BY id", (lan_key,))
    if not rows:
        return "ERROR: LAN not found (or already cancelled)"

    if action == "RETRY":
        errs = [r for r in rows if r["status"] == "ERROR"]
        if not errs:
            return f"SKIPPED: LAN is {rows[-1]['status']}, not ERROR"
        db.q("UPDATE orders SET status='PENDING', notes=%s, updated_at=now() WHERE id=%s",
             (f"retry requested | was: {errs[0]['notes']}", errs[0]["id"]))
        ctx.event("INFO", "Retry", f"LAN {errs[0]['lan']} retried by ops (ERROR → PENDING)")
        return "DONE: back to PENDING"

    if action == "KEEP_STATE":
        state = ctx.resolver.resolve(row[2])
        conf = [r for r in rows if r["status"] == "CONFLICT"]
        keep = [r for r in conf if r["state"] == state]
        if not conf:
            return f"SKIPPED: LAN is not in CONFLICT ({rows[-1]['status']})"
        if not keep:
            return f"ERROR: no CONFLICT row for state '{row[2]}' – options: {sorted({r['state'] for r in conf})}"
        with db.conn() as c, c.transaction():
            c.execute("UPDATE orders SET status='CANCELLED', notes='conflict resolved: other state kept', updated_at=now() "
                      "WHERE id = ANY(%s)", ([r["id"] for r in conf if r["id"] != keep[0]["id"]],))
            c.execute("UPDATE orders SET status='PENDING', notes='conflict resolved by ops', updated_at=now() WHERE id=%s",
                      (keep[0]["id"],))
        ctx.event("INFO", "KeepState", f"LAN {keep[0]['lan']} conflict resolved by ops: {state} kept, others cancelled")
        return f"DONE: {state} kept, others cancelled"

    if action == "CANCEL":
        o = rows[-1]
        if o["status"] == "ALLOCATING":
            return "RETRY LATER: LAN is being allocated right now"
        if o["status"] != "DONE":
            db.q("UPDATE orders SET status='CANCELLED', notes='cancelled by ops', updated_at=now() WHERE lan_key=%s "
                 "AND status <> 'CANCELLED'", (lan_key,))
            ctx.event("INFO", "Cancel", f"LAN {o['lan']} cancelled by ops (no paper had been issued)")
            return "DONE: cancelled (no paper had been issued)"
        p = db.q1("SELECT * FROM papers WHERE id=%s", (o["paper_id"],))
        a = db.q1("SELECT * FROM allocations WHERE order_id=%s AND cancelled_at IS NULL", (o["id"],))
        if not s.get("allocation.cancel_returns_paper", True):
            db.q("UPDATE orders SET status='CANCELLED', notes='cancelled by ops after issue; paper NOT returned', "
                 "updated_at=now() WHERE id=%s", (o["id"],))
            ctx.event("INFO", "Cancel", f"LAN {o['lan']} cancelled by ops after issue; paper {p['cert_no']} kept as used")
            return "DONE: cancelled; paper kept as used"
        # Record in DB before any Drive change (#11): commit the ledger first, then move the file back
        # to Inventory. A move failure after the commit is recoverable and only warns – the ledger is
        # already correct, so a crash here can never leave the paper issued-but-marked-returned.
        with db.conn() as c, c.transaction():
            c.execute("UPDATE papers SET status='AVAILABLE', order_id=NULL, file_name=%s, "
                      "notes=%s, updated_at=now() WHERE id=%s", (f"{p['cert_no']}.pdf", f"returned from LAN {o['lan']}", p["id"]))
            c.execute("UPDATE orders SET status='CANCELLED', notes='cancelled by ops – paper returned to stock', "
                      "updated_at=now() WHERE id=%s", (o["id"],))
            if a:
                c.execute("UPDATE allocations SET cancelled_at=now() WHERE id=%s", (a["id"],))
        try:
            g.move_rename(p["drive_file_id"], g.folder_path(s.folder(INVENTORY_ROOT), [p["state"], str(p["denomination"])]),
                          f"{p['cert_no']}.pdf")
        except Exception as e:  # ledger already consistent; the file can be relocated from wherever it is
            ctx.event("WARN", "Cancel", f"LAN {o['lan']} cancelled; paper {p['cert_no']} marked AVAILABLE but could not "
                      f"be moved back to Inventory: {e}")
        if a:
            # The "CANCELLED – do not use" row is the legal marker on Shriram's sheet. It is written after the
            # commit, so if the sheet write fails it must be LOUD (ops add it by hand), never silently dropped.
            try:
                sid = output_sheet_id(ctx, a["alloc_date"], a["state"], a["denomination"])
                g.append_rows(sid, label_for(a["state"], a["denomination"]),
                              [[o["lan"], p["cert_no"], "", "", ctx.now_str(), "CANCELLED – do not use"]])
            except Exception as e:
                ctx.event("ERROR", "Cancel", f"LAN {o['lan']} cancelled and paper {p['cert_no']} returned, but writing "
                          f"the 'CANCELLED – do not use' row to the {label_for(a['state'], a['denomination'])} output "
                          f"sheet failed: {e}. Add it by hand so Shriram does not treat {p['cert_no']} as valid.")
        ctx.event("WARN", "Cancel", f"LAN {o['lan']} cancelled after issue; paper {p['cert_no']} returned to stock. "
                  "Make sure Shriram has not already used the issued PDF.")
        return "DONE: cancelled, paper returned to stock"

    return "ERROR: Action must be CANCEL, RETRY or KEEP_STATE"


def recheck_review(ctx: Ctx) -> tuple[int, int]:
    """Re-run the checks on every NEEDS_REVIEW paper using its saved reading – the OCR API's fields, or the saved
    text (no re-upload, no new OCR). Use after a config fix (new pattern, corrected amount). Passing papers move into
    Inventory. Papers held for a file-level reason (E105/E107 pairing) are not rechecked: they need a re-upload."""
    from .extract import analyze_fields, analyze_paper
    db, g, s = ctx.db, ctx.google, ctx.settings
    moved = still = 0
    for p in db.q("SELECT * FROM papers WHERE status='NEEDS_REVIEW' ORDER BY id"):
        fields = (p.get("ocr_raw") or {}).get("fields")
        if (not p["ocr_text"] and fields is None) or (p["notes"] or "").startswith(("E105", "E107")):
            still += 1
            continue
        if fields is not None:
            a = analyze_fields(fields, s, ctx.matcher, db.cert_is_live, ctx.resolver)
        else:
            a = analyze_paper(p["ocr_text"], "", s, ctx.matcher, db.cert_is_live, ctx.resolver)
        if not a.ok:
            db.update_paper(p["id"], notes="; ".join(a.issues))
            still += 1
            continue
        try:
            db.update_paper(p["id"], cert_no=a.cert, cert_state=a.cert_state, state=a.state, denomination=a.amount,
                            ocr_amount=a.amount, status="AVAILABLE", notes=f"rechecked OK | was: {p['notes']}")
        except UniqueViolation:
            db.update_paper(p["id"], notes=f"duplicate certificate {a.cert} – already in stock")
            still += 1
            continue
        g.move_rename(p["drive_file_id"], g.folder_path(s.folder(INVENTORY_ROOT), [a.state, str(a.amount)]),
                      f"{a.cert}.pdf")
        db.update_paper(p["id"], file_name=f"{a.cert}.pdf")
        moved += 1
    ctx.event("INFO", "Recheck", f"{moved} paper(s) moved from Needs Review into Inventory, {still} still need review")
    return moved, still
