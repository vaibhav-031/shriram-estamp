"""One cycle, in this order (each step is safe to re-run):

  1. recover   – LANs/papers stuck mid-allocation after a crash → ERROR (never silently retried)
  2. actions   – ops approvals / cancellations from the control sheet
  3. ingest    – new LAN sheets → PENDING orders
  4. allocate  – PENDING LANs ← AVAILABLE papers (uses stock already on hand first)
  5. intake    – Upload Inbox → split → OCR → Inventory / Needs Review (time-boxed)
  6. allocate  – again, so freshly stocked papers are issued in the same cycle
  7. outputs   – Output sheets for everything issued
  8. alerts    – low stock, cycle summary (errors/warnings), daily summary, heartbeat
  9. dashboard – control sheet tabs
  (after low stock: retention – once a day, clear stored page text / events past their window, H-1; not in dry run)
"""
from __future__ import annotations

import time
import traceback

from .actions import run_actions
from .allocate import run_allocation
from .context import Ctx, log
from .dashboard import low_stock_alerts, run_dashboard
from .intake import run_intake
from .orders import run_ingest
from .outputs import run_outputs


def run_cycle(ctx: Ctx) -> bool:
    s, db = ctx.settings, ctx.db
    if not db.try_lock():
        # Another cycle holds the lock. A brief overlap is normal, but a run still going long past a normal
        # cycle means the holder is wedged and silently blocking all work – escalate so ops hear it (#3).
        age = db.active_run_age_seconds()
        wedged = float(s.get("alerts.wedged_run_minutes", 30)) * 60
        if not ctx.dry and age is not None and age > wedged:
            ctx.event("ERROR", "Cycle", f"a cycle has been running {age / 60:.0f} min and is blocking every new "
                      "cycle – the process is likely wedged. Restart `python -m estamp run`.", alert_key="cycle_wedged")
            try:
                _send(ctx)              # we return early, so the normal end-of-cycle delivery never runs
                ctx.heartbeat(False)    # tell the external monitor we are not healthy
            except Exception as e:
                log.error(f"wedged-cycle alert failed: {e}")
        else:
            log.warning("another run is in progress – skipping this cycle")
        return True
    run_id = db.start_run()
    ok = True
    t0 = time.time()
    budget = float(s.get("cycle.max_seconds", 900))
    try:
        if not ctx.dry:
            o, p = db.recover_stuck(int(s.get("alerts.stuck_minutes", 15)))
            if o or p:
                ctx.event("ERROR", "Recovery", f"{o} LAN(s) and {p} paper(s) were stuck mid-allocation (crash?) – "
                          "moved to ERROR. Check Output/Backup folders, then RETRY in Order Actions.")
        _step(ctx, "Actions", lambda: run_actions(ctx))
        _step(ctx, "Ingest", lambda: run_ingest(ctx, t0 + budget * 0.2))
        _step(ctx, "Allocate", lambda: run_allocation(ctx, t0 + budget * 0.45, report_shortage=False))
        _step(ctx, "Intake", lambda: run_intake(ctx, t0 + budget * 0.8))
        _step(ctx, "Allocate", lambda: run_allocation(ctx, t0 + budget * 0.95))
        _step(ctx, "Outputs", lambda: run_outputs(ctx))
        _step(ctx, "Low stock", lambda: low_stock_alerts(ctx))
        if not ctx.dry:
            _step(ctx, "Retention", lambda: _prune(ctx))
    except Exception as e:
        ok = False
        ctx.event("ERROR", "Cycle", f"cycle crashed: {e}\n{traceback.format_exc()[-1500:]}")
    finally:
        try:
            _step(ctx, "Dashboard", lambda: run_dashboard(ctx))
        finally:
            # Each wrap-up call is independent: a failure in one must not skip the others, and
            # release_lock() MUST always run or the advisory lock wedges every future cycle (#10).
            try:
                db.finish_run(run_id, ok, ctx.stats.as_dict())
            except Exception as e:
                log.error(f"finish_run failed: {e}")
            try:
                _send(ctx)
            except Exception as e:
                log.error(f"sending alerts failed: {e}")
            try:
                ctx.heartbeat(ok)
            except Exception as e:
                log.error(f"heartbeat failed: {e}")
            db.release_lock()
    st = ctx.stats
    ctx.event("INFO", "Cycle", f"summary: received {st.uploads} · stocked {st.papers_in} · in review {st.papers_review} "
              f"· issued {st.allocated} · waiting {st.waiting} · errors {st.errors}")
    log.info(f"cycle done in {time.time() - t0:.0f}s: {ctx.stats.as_dict()}")
    return ok


def _prune(ctx: Ctx) -> None:
    """H-1 retention: stored page text after retention.ocr_text_days (OCR API fields kept), events after
    retention.events_days. Checked every cycle, but the scan runs once a day (windows are in days; a failed run is
    retried the next day). Idempotent."""
    s = ctx.settings
    if not ctx.db.alert_once("retention", ctx.today()):
        return

    def days(key: str, default: int) -> int:
        v = s.get(key)
        return default if v is None else int(v)   # missing/blank → default; 0 → that window off

    papers, events = ctx.db.prune_retention(days("retention.ocr_text_days", 30), days("retention.events_days", 90))
    if papers or events:
        ctx.event("INFO", "Retention", f"cleared stored page text of {papers} paper(s), deleted {events} old event(s)")


def _step(ctx: Ctx, name: str, fn) -> None:
    try:
        fn()
    except Exception as e:
        ctx.event("ERROR", name, f"step failed: {e}\n{traceback.format_exc()[-1500:]}")


def _send(ctx: Ctx) -> None:
    s, st = ctx.settings, ctx.stats
    name = s.get("client_name", "Shriram")
    if ctx.alerts and (not ctx.dry or s.get("alerts.in_dry_run", False)):
        lines = [f"Issued {st.allocated} · LANs in {st.orders_in} · papers in {st.papers_in} · review {st.papers_review} · "
                 f"errors {st.errors} · waiting {st.waiting}", ""] + [f"• {a}" for a in ctx.alerts[:40]]
        if len(ctx.alerts) > 40:
            lines.append(f"… {len(ctx.alerts) - 40} more – see the Log tab")
        ctx.send_alerts(f"[{name} eStamp] {st.errors} error(s), {len(ctx.alerts)} alert(s)", lines)

    hour = int(s.get("alerts.daily_summary_hour", 20))
    if not ctx.dry and ctx.now().hour >= hour and ctx.db.alert_once("daily_summary", ctx.today()):
        from .dashboard import stock_rows
        d = ctx.today()
        c = ctx.db.q1("""SELECT (SELECT count(*) FROM orders WHERE (received_at AT TIME ZONE %(tz)s)::date=%(d)s) rec,
                                (SELECT count(*) FROM allocations WHERE alloc_date=%(d)s AND cancelled_at IS NULL) iss,
                                (SELECT count(*) FROM orders WHERE status='PENDING') pend,
                                (SELECT count(*) FROM orders WHERE status IN ('ERROR','CONFLICT')) exc,
                                (SELECT count(*) FROM papers WHERE status='NEEDS_REVIEW') rev""", {"d": d, "tz": str(ctx.tz)})
        lines = [f"Date {d}", f"LANs received {c['rec']} · issued {c['iss']} · pending {c['pend']} · "
                 f"exceptions {c['exc']} · papers in review {c['rev']}", "", "Stock flags:"]
        lines += [f"  {r[0]} ₹{r[2]}: avail {r[4]}, pending {r[6]}, net {r[7]}, cover {r[8]}d → {r[13]}"
                  for r in stock_rows(ctx) if r[13] != "OK"] or ["  all OK"]
        ctx.send_alerts(f"[{name} eStamp] Daily summary {d}", lines)


def run_forever(ctx_factory, interval: int) -> None:
    while True:
        ctx = ctx_factory()
        try:
            run_cycle(ctx)
        except Exception:
            log.exception("cycle failed outside the safety net")
        time.sleep(interval)
