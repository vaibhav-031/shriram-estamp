"""Command line.

  python -m estamp login          one-time Google sign-in (opens your browser)
  python -m estamp init-db        create the shriram_estamp schema + tables (safe to re-run)
  python -m estamp reset-test-data clear all ledger rows in the current schema (typed YES; refuses prod)
  python -m estamp check-setup    verify DB, Google access, every folder, state config, APIs, alerts
  python -m estamp run-once       one full cycle (respects dry_run in config.yaml)
  python -m estamp run            keep running a cycle every loop_seconds (refuses in dry run)
  python -m estamp status         stock + queue summary in the terminal
  python -m estamp report         build the Daily Report + Vendor Reconciliation sheet (--date / --from --to)
  python -m estamp reconcile      find inventory files the ledger doesn't know about (and vice-versa)
  python -m estamp recheck-review re-run checks on Needs Review papers after a config fix (no re-upload)
  python -m estamp explain E111   print what an error code means and what ops should do (no DB/Google)
"""
from __future__ import annotations

import argparse
import sys

from .constants import (BACKUP_ROOT, INVENTORY_ROOT, NEEDS_REVIEW, ORDERS_INCOMING, ORDERS_PROCESSED,
                        ORDERS_REJECTED, OUTPUT_ROOT, RECONCILIATION_ROOT, UPLOAD_ARCHIVE, UPLOAD_INBOX)
from .context import Ctx, alert_channels, safe_console, setup_logging
from .db import DB
from .settings import load_settings


def _build(args, need_google=True):
    s = load_settings(args.config)
    if args.dry_run:
        s.raw["dry_run"] = True
    if args.live:
        s.raw["dry_run"] = False
    setup_logging(s)
    db = DB(s.env("db.url_env"), s.schema, max_size=int(s.get("workers", 8)) + 4)
    google = None
    if need_google:
        from .google_clients import Google, WrongGoogleAccount
        try:
            google = Google(s)
        except WrongGoogleAccount as e:
            raise SystemExit(str(e))
    return s, db, google


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="estamp")
    ap.add_argument("command", choices=["login", "init-db", "reset-test-data", "check-setup", "run-once", "run", "status", "report", "reconcile", "recheck-review", "explain"])
    ap.add_argument("code", nargs="?", help="error code for `explain` (e.g. E111)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--date", help="report: single issue date YYYY-MM-DD (default today)")
    ap.add_argument("--from", dest="date_from", help="report: range start YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", help="report: range end YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="force dry run for this command")
    ap.add_argument("--live", action="store_true", help="force live mode for this command")
    ap.add_argument("--yes", action="store_true", help="reset-test-data: skip the typed confirmation (scripted use)")
    ap.add_argument("--force", action="store_true", help="reset-test-data: allow a production-looking schema")
    args = ap.parse_args(argv)
    safe_console()   # before any print: explain / login / check-setup print ✓ ✗ …

    if args.command == "explain":
        # No DB/Google – just the catalogue (like login, works offline).
        from .errors import explain
        if not args.code:
            ap.error("explain needs an error code, e.g. `explain E111`")
        print(explain(args.code))
        return 0

    if args.command == "login":
        from . import google_clients
        s = load_settings(args.config)
        token = s.path("google.oauth_token_file")
        for attempt in (1, 2):   # 1: cached token (if any); 2: fresh browser sign-in after dropping a wrong one
            try:
                g = google_clients.Google(s, credentials=google_clients.load_credentials(s))
                break
            except google_clients.WrongGoogleAccount as e:
                # Never leave a wrong-account token behind – every later command would pick it up.
                token.unlink(missing_ok=True)
                if attempt == 2:
                    raise SystemExit(f"{e}\n(The token for the wrong account was removed.)")
                print(f"{e}\nOpening the browser to sign in again…")
        print(f"Google sign-in OK as {g.email} – token saved to", token)
        return 0

    if args.command == "init-db":
        s, db, _ = _build(args, need_google=False)
        db.init_schema()
        print(f"Schema '{s.schema}' ready.")
        return 0

    if args.command == "reset-test-data":
        s, db, _ = _build(args, need_google=False)
        try:
            return _reset_test_data(s, db, confirm=args.yes, force=args.force)
        finally:
            db.close()

    s, db, google = _build(args, need_google=args.command not in ("status",))
    try:
        if args.command == "check-setup":
            return _check(s, db, google)
        _require_current_schema(s, db, warn_only=args.command == "status")
        if args.command == "status":
            return _status(s, db)
        if args.command == "report":
            return _report(s, db, google, args)
        if args.command == "reconcile":
            return _reconcile(s, db, google)
        if args.command == "recheck-review":
            from .actions import recheck_review
            moved, still = recheck_review(Ctx(s, db, google))
            print(f"{moved} paper(s) moved into Inventory, {still} still in Needs Review")
            return 0
        from .cycle import run_cycle, run_forever
        if args.command == "run-once":
            ok = run_cycle(Ctx(s, db, google))
            return 0 if ok else 1
        if args.command == "run":
            if s.dry_run:
                print("dry_run is ON – use `run-once` to preview, or set dry_run: false to run continuously.")
                return 2
            run_forever(lambda: Ctx(s, db, google), int(s.get("loop_seconds", 120)))
    finally:
        db.close()
    return 0

# Wipe the ledger in the CURRENT schema (go-live test-data reset). DB only – Drive files are handled separately.
# Guards: refuse a production-looking schema unless --force; require a typed YES unless --yes.
PROD_SCHEMAS = {"shriram_estamp"}


def _reset_test_data(s, db, confirm: bool, force: bool) -> int:
    from urllib.parse import urlsplit
    host = urlsplit(db.url).hostname or "?"          # host only – never print the URL (it may hold credentials)
    schema = s.schema

    if schema in PROD_SCHEMAS and not force:
        print(f"REFUSING: schema '{schema}' looks like production. "
              f"Re-run with --force only if you really mean to wipe it.")
        return 2

    counts = db.table_counts()
    print(f"About to wipe schema '{schema}' on host '{host}'.")
    print(f"Row counts ({sum(counts.values())} rows across {len(counts)} tables):")
    for t, n in counts.items():
        print(f"  {t:<16}{n}")

    if not confirm:
        if input("Type YES to clear all of the above (anything else aborts): ") != "YES":
            print("cancelled, nothing changed")
            return 1

    cleared = db.reset_all_data()
    print(f"Cleared {sum(cleared.values())} rows from {len(cleared)} tables in '{schema}'.")
    print("Reminder: Drive test files are NOT touched by this command – clean them up separately.")
    return 0


# config and env check
def _require_current_schema(s, db, warn_only: bool = False) -> None:
    """Stop before any work if the ledger is behind the code (e.g. pulled without running init-db).
    warn_only: read-only diagnostics (`status`) still run – that's when ops need them most."""
    missing = db.missing_schema()
    if missing:
        msg = (f"Postgres schema '{s.schema}' is out of date – missing: {', '.join(missing)}.\n"
               f"Run `python -m estamp init-db` first (additive only: adds what is missing, changes no rows).")
        if not warn_only:
            raise SystemExit(msg)
        print("WARNING:", msg, "\n")


def _check(s, db, google) -> int:
    ok = True

    def line(good: bool, label: str, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print(f"  {'✓' if good else '✗'} {label}{(' – ' + detail) if detail else ''}")

    print("Mode:", "DRY RUN" if s.dry_run else "LIVE")
    try:
        missing = db.missing_schema()
        line(not missing, f"Postgres schema '{s.schema}'",
             "up to date" if not missing else f"missing {', '.join(missing)} – run init-db")
    except Exception as e:
        line(False, "Postgres", str(e))
    from . import google_clients
    try:
        line(True, "Google account", google.email + ("" if google_clients.shared_account(s)
                                                     else " (google.account not enforced – any account allowed)"))
    except Exception as e:
        line(False, "Google account", str(e)[:150])
    for key in [UPLOAD_INBOX, UPLOAD_ARCHIVE, NEEDS_REVIEW, INVENTORY_ROOT, ORDERS_INCOMING,
                ORDERS_PROCESSED, ORDERS_REJECTED, OUTPUT_ROOT, BACKUP_ROOT, RECONCILIATION_ROOT]:
        try:
            f = google.get_file(s.folder(key))
            line(not f.get("trashed"), f"folder {key}", f"'{f['name']}'")
        except Exception as e:
            line(False, f"folder {key}", str(e)[:150])
    sid = s.get("control_sheet_id")
    try:
        line(bool(sid), "control sheet", ", ".join(google.tab_titles(sid)) if sid else "control_sheet_id not set")
    except Exception as e:
        line(False, "control sheet", str(e)[:150])
    act = [c for c, st in s.states.items() if st.active]
    ina = [f"{c} ({st.notes})" for c, st in s.states.items() if not st.active]
    line(bool(act), "state config", f"{len(act)} active: {', '.join(act)}" + (f" | inactive: {'; '.join(ina)}" if ina else ""))
    line(True, "daily projection", f"{sum(st.daily_qty for st in s.states.values() if st.active)} LANs/day (active states)")
    line(True, "split", s.get("intake.split_provider", "local"))
    ocr = s.get("intake.ocr_provider", "drive")
    if ocr == "zoop_api":
        ocr_url = s.env("zoop_api.ocr.url_env") or s.get("zoop_api.ocr.url")
        src = "env ESTAMP_OCR_URL" if s.env("zoop_api.ocr.url_env") else "config (dev fallback)"
        line(bool(ocr_url) and bool(s.env("zoop_api.ocr.api_key_env")), "OCR: in-house API",
             f"url via {src} + key set" if ocr_url else "OCR url missing – set ESTAMP_OCR_URL in .env")
    else:
        line(True, "OCR: Google Drive OCR")
    # A delivery channel (Slack/email) is what carries failure alerts; heartbeat is only a liveness
    # ping and can't deliver a message, so it doesn't count here (same rule as send_alerts, #2).
    channels = alert_channels(s)
    line(bool(channels), "alerts", ", ".join(channels) or "none configured – you will not hear about failures")
    line(True, "heartbeat", "configured" if s.env("alerts.heartbeat_url_env") else "not set (optional)")
    print("\nAll good." if ok else "\nFix the ✗ items above.")
    return 0 if ok else 1

# current status of the jobs
def _status(s, db) -> int:
    from .context import Ctx
    ctx = Ctx(s, db, None)
    from .dashboard import stock_rows
    print("Stock per state/denomination:")
    for abbr, meaning in (
        ("ST", "state"),
        ("₹", "denomination"),
        ("daily", "daily issue rate"),
        ("avail", "papers ready"),
        ("review", "awaiting human"),
        ("pending", "LANs waiting for stock"),
        ("net", "avail − pending"),
        ("cover", "days of stock left"),
        ("flag", "OK / LOW / SHORT / INACTIVE"),
    ):
        print(f"  {abbr:<8}{meaning}")
    print()
    print(f"{'ST':<4}{'₹':>6}{'daily':>7}{'avail':>7}{'review':>8}{'pending':>9}{'net':>7}{'cover':>7}  flag")
    for r in stock_rows(ctx):
        print(f"{r[0]:<4}{r[2]:>6}{r[3]:>7}{r[4]:>7}{r[5]:>8}{r[6]:>9}{r[7]:>7}{str(r[8]):>7}  {r[13]}")
    for r in db.q("SELECT status, count(*) n FROM orders GROUP BY 1 ORDER BY 1"):
        print(f"orders {r['status']:<11} {r['n']}")
    return 0


# Daily Report + Vendor Reconciliation (D-2/D-3). Read-only against the ledger; writes one Google Sheet.
def _report(s, db, google, args) -> int:
    from datetime import date

    from .context import Ctx
    from .report import run_report

    def parse(label, value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise SystemExit(f"{label} must be YYYY-MM-DD, got '{value}'")

    if args.date_from or args.date_to:
        if not (args.date_from and args.date_to):
            raise SystemExit("use --from AND --to together for a range (or --date for one day)")
        d_from, d_to = parse("--from", args.date_from), parse("--to", args.date_to)
        if d_from > d_to:
            raise SystemExit("--from must not be after --to")
    else:
        d = parse("--date", args.date) if args.date else Ctx(s, db, google).today()
        d_from = d_to = d

    sid = run_report(Ctx(s, db, google), d_from, d_to)
    from .google_clients import file_link
    print("Daily Report:", file_link(sid))
    return 0


def _reconcile(s, db, google) -> int:
    known = {r["drive_file_id"] for r in db.q("SELECT drive_file_id FROM papers WHERE drive_file_id IS NOT NULL")}
    root = s.folder(INVENTORY_ROOT)
    orphans = []
    for st in google.list_children(root, folders=True):
        for dn in google.list_children(st["id"], folders=True):
            for f in google.list_children(dn["id"], folders=False):
                if f["id"] not in known:
                    orphans.append(f"{st['name']}/{dn['name']}/{f['name']}")
    print(f"Files in Inventory that the ledger does not know ({len(orphans)}):")
    for o in orphans[:500]:
        print("  ", o)
    stuck = db.q("SELECT id, lan, status, updated_at FROM orders WHERE status IN ('ALLOCATING','ERROR') ORDER BY id")
    print(f"\nLANs in ALLOCATING/ERROR ({len(stuck)}):")
    for r in stuck[:500]:
        print(f"   {r['lan']}  {r['status']}  {r['updated_at']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
