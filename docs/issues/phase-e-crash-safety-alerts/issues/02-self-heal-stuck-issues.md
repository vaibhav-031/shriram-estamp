# 02: Self-heal stuck issues from Drive evidence (ADR-0001)

Status: ready-for-agent

**What to build:** at cycle start, LANs stuck mid-issue for more than N minutes are resolved by where the paper's
file really is: in Inventory → release (paper AVAILABLE, LAN PENDING); in Output as `<LAN>.pdf` → finish the issue
(backup if missing, issue check, ledger, DONE); elsewhere or missing → ERROR + alert. Each decision logged E306.

**Blocked by:** Phase D complete

- [ ] One cycle test per case (Inventory / Output / missing)
- [ ] "Finish" runs the issue check; a mismatch follows the re-map rules once ticket 03 lands
- [ ] Cycle/issue module docstrings amended to name exactly the allowed automatic actions and point to ADR-0001 (CLAUDE.md already amended 2026-10-27)
- [ ] `reconcile` output updated to match (no longer the only way to find stuck LANs)

## Comments
