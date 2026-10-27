---
status: accepted
---

# Limited automatic recovery at issue (re-map once, self-heal from Drive evidence)

The pilot rule was "anything uncertain goes to ERROR for a human, never silently retried or reassigned". At
~10.8k LANs/day that leaves ops clearing crash leftovers and OCR blips by hand, so two automatic actions are now
allowed, and only these two (decided 2026-10-27, HANDOFF D7 + E-1):

1. **Re-map once.** If the issue check fails, the paper is quarantined and the same LAN gets the next Inventory
   paper. A second failure on the same LAN puts it in ERROR for a human (E302 → E303).
2. **Self-heal stuck issues.** At cycle start, a LAN stuck mid-issue is resolved from where its file really is in
   Drive: still in Inventory → release; already in Output as `<LAN>.pdf` → finish the issue (backup, issue check,
   ledger); anywhere else or missing → ERROR + alert (E306).

Every automatic decision is logged with its error code. Everything else that is uncertain still goes to ERROR /
Needs Review. CLAUDE.md's safety rule points here (amended 2026-10-27); the module docstrings that still say
"nothing is auto-reassigned" change when Phase E builds these actions.
