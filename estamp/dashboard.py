"""Control sheet = the window ops and you look at. Postgres is the source of truth; these tabs are rewritten
every cycle (except the two Actions tabs, which ops write into).

Tabs: Summary · Stock · Needs Review · Exceptions · Corrections · Log · Dry Run Preview
"""
from __future__ import annotations

from .context import Ctx
from .google_clients import file_link

PREVIEW_HEADER = ["Type", "File", "Where", "Cert / LAN", "State", "Stamp Duty", "Destination / Result", "Notes"]


def stock_rows(ctx: Ctx) -> list[list]:
    db, today = ctx.db, ctx.today()
    papers = {(r["state"], r["denomination"], r["status"]): r["n"] for r in
              db.q("SELECT state, denomination, status, count(*) n FROM papers GROUP BY 1,2,3")}
    pending = {(r["state"], r["denomination"]): r["n"] for r in
               db.q("SELECT state, denomination, count(*) n FROM orders WHERE status='PENDING' GROUP BY 1,2")}
    issued = {(r["state"], r["denomination"]): r["n"] for r in
              db.q("SELECT state, denomination, count(*) n FROM allocations WHERE alloc_date=%s AND cancelled_at IS NULL "
                   "GROUP BY 1,2", (today,))}
    rows = []
    for code, st in sorted(ctx.settings.states.items()):
        for amt in st.amounts:
            avail = papers.get((code, amt, "AVAILABLE"), 0)
            pend = pending.get((code, amt), 0)
            net = avail - pend
            cover = round(avail / st.daily_qty, 1) if st.daily_qty else ""
            flag = ("INACTIVE" if not st.active else "SHORT" if net < 0 else
                    "LOW" if net < st.low_stock_threshold else "OK")
            rows.append([code, st.name, amt, st.daily_qty, avail, papers.get((code, amt, "NEEDS_REVIEW"), 0), pend, net,
                         cover, issued.get((code, amt), 0), papers.get((code, amt, "ASSIGNED"), 0),
                         papers.get((code, amt, "ERROR"), 0), st.low_stock_threshold, flag])
    return rows


def low_stock_alerts(ctx: Ctx) -> None:
    for r in stock_rows(ctx):
        code, _, amt, daily, avail, _, pend, net, cover, *_rest, threshold, flag = r
        if flag in ("LOW", "SHORT"):
            ctx.event("WARN", "Low stock", f"{code} ₹{amt}: {avail} available, {pend} pending → net {net} "
                      f"(alert below {threshold} = {ctx.settings.states[code].buffer_days} day(s) of {daily}). "
                      "Raise a vendor order.", alert_key=f"low|{code}|{amt}")


def run_dashboard(ctx: Ctx) -> None:
    sid = ctx.settings.get("control_sheet_id")
    if not sid:
        return
    g, db = ctx.google, ctx.db
    tabs = {
        "Summary": ["Metric", "Value"],
        "Stock": ["State", "Name", "Stamp Duty", "Daily Qty", "Available", "Needs Review", "Pending LANs",
                  "Net After Pending", "Days of Cover", "Issued Today", "Issued (all time)", "Error", "Alert Below", "Flag"],
        "Needs Review": ["Paper ID", "Upload", "Vendor", "Page", "Cert No (OCR)", "State (OCR)", "Stamp Duty (OCR)",
                         "Reason", "Open PDF", "Added At"],
        "Exceptions": ["LAN ID", "State", "Stamp Duty", "Status", "Notes", "Source File", "Updated At"],
        "Corrections": ["Paper ID", "API read (cert/state/₹)", "Ops value (cert/state/₹)", "Edited By", "Edited At",
                        "LAN issued to"],
        "Log": ["Time", "Level", "Action", "Details"],
    }
    if ctx.dry:
        tabs["Dry Run Preview"] = PREVIEW_HEADER
    g.ensure_tabs(sid, tabs)
    t = lambda ts: ts.astimezone(ctx.tz).strftime("%Y-%m-%d %H:%M:%S") if ts else ""

    today = ctx.today()
    counts = db.q1("""SELECT
        (SELECT count(*) FROM orders WHERE (received_at AT TIME ZONE %(tz)s)::date = %(d)s) AS received_today,
        (SELECT count(*) FROM allocations WHERE alloc_date = %(d)s AND cancelled_at IS NULL) AS issued_today,
        (SELECT count(*) FROM orders WHERE status='PENDING') AS pending,
        (SELECT count(*) FROM orders WHERE status IN ('ERROR','CONFLICT')) AS exceptions,
        (SELECT count(*) FROM papers WHERE status='AVAILABLE') AS available,
        (SELECT count(*) FROM papers WHERE status='NEEDS_REVIEW') AS review,
        (SELECT count(*) FROM papers WHERE (added_at AT TIME ZONE %(tz)s)::date = %(d)s) AS papers_today,
        (SELECT max(finished_at) FROM runs WHERE ok) AS last_ok_run""", {"d": today, "tz": str(ctx.tz)})
    g.overwrite_tab(sid, "Summary", [tabs["Summary"],
        ["Mode", "DRY RUN – nothing moved" if ctx.dry else "LIVE"],
        ["Last updated", ctx.now_str()],
        ["Last successful run", t(counts["last_ok_run"])],
        ["LANs received today", counts["received_today"]],
        ["LANs issued today", counts["issued_today"]],
        ["LANs pending (waiting for stock)", counts["pending"]],
        ["LANs in ERROR / CONFLICT (see Exceptions)", counts["exceptions"]],
        ["Papers taken in today", counts["papers_today"]],
        ["Papers available", counts["available"]],
        ["Papers needing review (see Needs Review)", counts["review"]],
        ["How to act", "Approve/reject papers in 'Review Actions'; cancel/retry/resolve LANs in 'Order Actions'"]])

    g.overwrite_tab(sid, "Stock", [tabs["Stock"]] + stock_rows(ctx))

    review = db.q("""SELECT p.*, u.file_name AS upload_name FROM papers p LEFT JOIN uploads u ON u.id = p.upload_id
                     WHERE p.status='NEEDS_REVIEW' ORDER BY p.added_at LIMIT 3000""")
    g.overwrite_tab(sid, "Needs Review", [tabs["Needs Review"]] + [
        [r["id"], r["upload_name"], r["vendor_code"] or "", f"{r['page_from']}-{r['page_to']}", r["cert_no"],
         r["state"], r["denomination"], r["notes"], file_link(r["drive_file_id"]) if r["drive_file_id"] else "",
         t(r["added_at"])] for r in review])

    exc = db.q("SELECT * FROM orders WHERE status IN ('ERROR','CONFLICT') ORDER BY updated_at DESC LIMIT 3000")
    g.overwrite_tab(sid, "Exceptions", [tabs["Exceptions"]] + [
        [r["lan"], r["state"], r["denomination"], r["status"], r["notes"], r["source_file"], t(r["updated_at"])] for r in exc])

    corr = db.q("""SELECT p.*, a.lan FROM papers p
                   LEFT JOIN allocations a ON a.paper_id = p.id AND a.cancelled_at IS NULL
                   WHERE p.ops_edited_at IS NOT NULL ORDER BY p.ops_edited_at DESC LIMIT 3000""")
    fmt = lambda c, st, amt: f"{c or ''} / {st or ''} / ₹{amt or ''}"
    g.overwrite_tab(sid, "Corrections", [tabs["Corrections"]] + [
        [r["id"], fmt(r["api_cert_no"], r["api_state"], r["api_denomination"]),
         fmt(r["cert_no"], r["state"], r["denomination"]), r["ops_edited_by"], t(r["ops_edited_at"]), r["lan"] or ""]
        for r in corr])

    ev = db.q("SELECT * FROM events ORDER BY id DESC LIMIT 500")
    g.overwrite_tab(sid, "Log", [tabs["Log"]] + [[t(e["ts"]), e["level"], e["action"], e["details"][:2000]] for e in ev])

    if ctx.dry:
        g.overwrite_tab(sid, "Dry Run Preview", [PREVIEW_HEADER] + ctx.preview[:5000])
