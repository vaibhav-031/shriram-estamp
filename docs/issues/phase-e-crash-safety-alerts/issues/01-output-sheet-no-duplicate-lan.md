# 01: Never append the same LAN twice to an output sheet

Status: ready-for-agent

**What to build:** before appending issue rows, the service checks the target output sheet for LANs already
present and skips them, so a crash between "row written" and "recorded as written" can't produce a duplicate.

**Blocked by:** Phase D complete

- [x] Test: fault after append, before mark-written → next cycle writes no duplicate row and marks it written
- [x] CANCELLED rows for a LAN are still allowed after its ISSUED row

## Comments
