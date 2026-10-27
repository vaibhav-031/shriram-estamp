# 06: APPROVE with audited corrections; API reading shown in Needs Review

Status: ready-for-agent

**What to build:** Needs Review shows what the API read for each paper. APPROVE accepts an optional corrected
Paper ID and/or stamp duty, runs the same checks as intake, and stores API value, ops value, who and when. A new
Corrections tab lists every edit and the LAN the paper was later issued to.

**Blocked by:** Phase D complete

- [ ] Before the live tab changes: back up the existing Review Actions data (copy of the tab/sheet, nothing deleted); no cell ops entered is overwritten (Vaibhav, 2026-10-27)
- [ ] New Review Actions header per the spec; rows typed under the old header stay readable (migrated or kept in the backup), plan shown to Vaibhav before the live change
- [ ] "Edited by" source decided (sheet editor metadata vs a required name column) and recorded in Comments
- [ ] Tests: approve with ID edit, with amount edit, invalid ID for state, disallowed amount, duplicate ID – each with the right result text and audit fields
- [ ] Corrections tab rows and the "manually corrected" flag shown in reports

## Comments
