# 05: Manual Issues tab (system-down fallback)

Status: needs-triage

**What to build:** a Manual Issues tab (`LAN | Paper ID | Issued by | Result | Processed At`). When ops issued a
paper by hand, the service validates it (LAN PENDING, paper AVAILABLE, same state + stamp duty) and records it
exactly like an automatic issue, without moving files ops already placed. It never double-issues. The service fills
only Result and Processed At.

**Blocked by:** Phase D complete

- [ ] Tests: valid row recorded (ledger + output row); LAN already issued, paper not AVAILABLE, wrong state/amount → rejected with reason; re-run changes nothing
- [ ] The ops runbook row "System down, must issue by hand" in README matches the behaviour

## Comments
- 2026-10-27 Vaibhav: deferred – not on the approved shortlist (see HANDOFF.md → Current status).
