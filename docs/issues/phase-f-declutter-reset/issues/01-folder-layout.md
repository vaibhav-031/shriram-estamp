# 01: Numbered Drive layout with a System folder

Status: needs-triage

**What to build:** the target layout from HANDOFF §8 on the real Drive (numbered ops folders, vendor subfolders,
Reconciliation, "⚙️ System (don't touch)" holding Inventory, Backup, Upload Archive, Orders Processed, Orders
Rejected), proven by `check-setup`.

**Blocked by:** Phase E complete

**Safety:** real Drive moves – Vaibhav approves each move; move/rename only.

- [ ] `check-setup` passes after the moves
- [ ] A test with the in-memory Drive proves folder IDs still resolve after moves/renames
- [ ] README folder tree updated

## Comments
- 2026-10-27 Vaibhav: deferred – not on the approved shortlist (see HANDOFF.md → Current status).
