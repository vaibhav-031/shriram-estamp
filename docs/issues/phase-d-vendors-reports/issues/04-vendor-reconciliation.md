# 04: Vendor Reconciliation and Issued detail with balance check

Status: needs-triage

**What to build:** the report also writes a Vendor Reconciliation tab (per vendor × state × stamp duty: received,
issued, in Inventory, in Needs Review, rejected/duplicate; editable Rate per paper; formula Payable, Disputed, Net
payable; check row) and an Issued detail tab (LAN, state, stamp duty, Paper ID, vendor, issued at, output link).
A failed balance check raises E402.

**Blocked by:** 01, 03

- [ ] Received = issued + Inventory + Needs Review + rejected (+ errors) for every row; mismatch → E402
- [ ] Rate column left empty for ops; formulas work in Sheet and xlsx
- [ ] Tests: mixed outcomes, two amounts in one state, cancelled issue, forced imbalance → E402

## Comments
- 2026-10-27 Vaibhav: deferred – not on the approved shortlist (see HANDOFF.md → Current status).
