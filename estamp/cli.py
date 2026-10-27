"""Command line.

  python -m estamp login          one-time Google sign-in (opens your browser)
  python -m estamp init-db        create the shriram_estamp schema + tables (safe to re-run)
  python -m estamp check-setup    verify DB, Google access, every folder, state config, APIs, alerts
  python -m estamp run-once       one full cycle (respects dry_run in config.yaml)
  python -m estamp run            keep running a cycle every loop_seconds (refuses in dry run)
  python -m estamp status         stock + queue summary in the terminal
  python -m estamp reconcile      find inventory files the ledger doesn't know about (and vice-versa)
  python -m estamp recheck-review re-run checks on Needs Review papers after a config fix (no re-upload)
"""
from __future__ import annotations

import argparse
import sys

from .constants import (BACKUP_ROOT, INVENTORY_ROOT, NEEDS_REVIEW, ORDERS_INCOMING, ORDERS_PROCESSED,
                        ORDERS_REJECTED, OUTPUT_ROOT, UPLOAD_ARCHIVE, UPLOAD_INBOX)
from .context import Ctx, setup_logging
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
        from .google_clients import Google
        google = Google(s)
    return s, db, google


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="estamp")
    ap.add_argument("command", choices=["login", "init-db", "check-setup", "run-once", "run", "status", "reconcile", "recheck-review"])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true", help="force dry run for this command")
    ap.add_argument("--live", action="store_true", help="force live mode for this command")
    args = ap.parse_args(argv)

    if args.command == "login":
        from .google_clients import load_credentials
        s = load_settings(args.config)
        load_credentials(s)
        print("Google sign-in OK – token saved to", s.path("google.oauth_token_file"))
        return 0

    if args.command == "init-db":
        s, db, _ = _build(args, need_google=False)
        db.init_schema()
        print(f"Schema '{s.schema}' ready.")
        return 0

    s, db, google = _build(args, need_google=args.command not in ("status",))
    try:
        if args.command == "check-setup":
            return _check(s, db, google)
        if args.command == "status":
            return _status(s, db)
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

# config and env check
def _check(s, db, google) -> int:
    ok = True

    def line(good: bool, label: str, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print(f"  {'✓' if good else '✗'} {label}{(' – ' + detail) if detail else ''}")

    print("Mode:", "DRY RUN" if s.dry_run else "LIVE")
    try:
        n = db.q1("SELECT count(*) n FROM information_schema.tables WHERE table_schema=%s", (s.schema,))["n"]
        line(n >= 8, f"Postgres schema '{s.schema}'", f"{n} tables" + ("" if n >= 8 else " – run init-db"))
    except Exception as e:
        line(False, "Postgres", str(e))
    for key in [UPLOAD_INBOX, UPLOAD_ARCHIVE, NEEDS_REVIEW, INVENTORY_ROOT, ORDERS_INCOMING,
                ORDERS_PROCESSED, ORDERS_REJECTED, OUTPUT_ROOT, BACKUP_ROOT]:
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
        line(bool(s.get("zoop_api.ocr.url")) and bool(s.env("zoop_api.ocr.api_key_env")), "OCR: in-house API",
             "url + key set" if s.get("zoop_api.ocr.url") else "zoop_api.ocr.url missing")
    else:
        line(True, "OCR: Google Drive OCR")
    alerts = []
    if s.env("alerts.slack_webhook_env"):
        alerts.append("Slack")
    if (s.get("alerts.smtp") or {}).get("host"):
        alerts.append("email")
    if s.env("alerts.heartbeat_url_env"):
        alerts.append("heartbeat")
    line(bool(alerts), "alerts", ", ".join(alerts) or "none configured – you will not hear about failures")
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
