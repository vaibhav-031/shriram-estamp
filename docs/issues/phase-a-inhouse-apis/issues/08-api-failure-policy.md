# 08: API failure policy – stay in Inbox, then Needs Review; stop on auth failure

Status: ready-for-agent

**What to build:** when a page's read keeps failing after retries, the whole upload stays in the Upload Inbox and is
retried next cycle (nothing half-stocked). After the configured window (default 30 min) it moves to Needs Review
with the reason and an alert. A rejected key stops intake for the rest of the cycle with one loud error and no
retry storm.

**Blocked by:** 07

- [ ] Ledger tracks first failure time + failure count per upload (safe to re-run)
- [ ] Test: failing upload stays in Inbox, no paper rows stocked, retried next cycle; succeeds when the API recovers
- [ ] Test: after the window the upload is in Needs Review with a reason, and an alert is raised once
- [ ] Test: 401/403 → intake stops for the cycle, other steps (issue, outputs) still run, exactly one error event
- [ ] No silent fallback to any other reader (ADR-0002)

## Comments
