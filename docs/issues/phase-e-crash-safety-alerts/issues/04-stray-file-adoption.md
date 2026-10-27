# 04: Adopt stray files in Inventory

Status: needs-triage

**What to build:** every N cycles the service lists Inventory; any PDF the ledger doesn't know is read by the API
and checked, then registered AVAILABLE (vendor "unknown") or sent to Needs Review. "Stray file adopted" appears in
the Log tab. Nothing is ever deleted.

**Blocked by:** Phase D complete

- [ ] Interval in config
- [ ] Tests: valid stray → AVAILABLE; invalid stray → Needs Review; duplicate Paper ID → Needs Review (E126); re-run adopts nothing twice

## Comments
- 2026-10-27 Vaibhav: deferred – not on the approved shortlist (see HANDOFF.md → Current status).
