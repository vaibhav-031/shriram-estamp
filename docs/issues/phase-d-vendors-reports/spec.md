# Phase D – Vendors, money and reports

Status: ready-for-agent

Source: HANDOFF.md §6, decisions D5, D6.

## Problem Statement

The ops manager has to tell Shriram how many papers were issued per state and stamp duty, and the business has to
pay each vendor for what they delivered minus what was rejected. Today the service doesn't know which vendor a
paper came from, and there is no report – numbers would have to be typed by hand from the ledger.

## Solution

Every paper carries its vendor, taken from the vendor folder it was uploaded into. The control sheet shows a live
Daily Report; a `report` command freezes a dated copy as a Google Sheet + .xlsx in Reconciliation and prints its
link; the same command writes a Vendor Reconciliation with an editable rate per paper and formula columns, and
an Issued detail tab. All numbers come from the ledger, and the reconciliation must balance.

## User Stories

1. As ops, I want a subfolder per active vendor in the Upload Inbox, created by the service, so that I only drop files in the right folder.
2. As ops, I want a file dropped at the Inbox root (not in a vendor folder) left alone and flagged E101, so that no paper is stocked without a vendor.
3. As ops, I want files in nested folders inside a vendor folder to inherit that vendor.
4. As ops, I want the vendor shown in Stock, Needs Review and the Issued detail.
5. As the tech team, I want vendors in a `vendors.csv` (code, name, active), seeded with Paper Ease, Agreement Express and Shabda, so that adding a vendor is a config change.
6. As the ops manager, I want a live Daily Report tab showing today's papers issued to Shriram LANs per state and stamp duty, refreshed every cycle.
7. As the ops manager, I want each state + stamp duty on its own row (a state with two amounts gets two rows) and a total row.
8. As the ops manager, I want `python -m estamp report --date 2026-10-27` to create a frozen Google Sheet + .xlsx in Reconciliation/<date> and print the link, so that I can share it with Shriram.
9. As the ops manager, I want re-running the report for the same date to replace it (logged), so that there is one copy per date.
10. As the ops manager, I want `--from … --to …` for weekly/monthly totals.
11. As the ops manager, I want Amount and Total as sheet formulas, so that the sheet recalculates if I edit it.
12. As finance, I want per vendor × state × stamp duty: received, issued, in Inventory, in Needs Review, rejected/duplicate.
13. As finance, I want an editable "Rate per paper (₹)" column and formula columns Payable, Disputed and Net payable, so that I enter the rate and get the amount.
14. As finance, I want a check row (received = issued + Inventory + Needs Review + rejected + errors) that flags E402 if it doesn't balance.
15. As finance, I want cancelled issues not counted as issued (the paper is back in Inventory).
16. As ops, I want an Issued detail tab: LAN, state, stamp duty, Paper ID, vendor, issued at, output link.

## Implementation Decisions

- **Vendors config:** `vendors.csv` loaded with settings; the service ensures one Inbox subfolder per active vendor (by name), stored by folder ID once created.
- **Vendor of a paper:** resolved from the top-level vendor folder the upload sits under; stored on uploads and papers (schema change, existing rows = "unknown").
- **E101:** root-level files are left in place, recorded once, and flagged; not stocked.
- **Report source:** issued = live (not cancelled) issues by issue date within the date/range; counts from the ledger only.
- **Report outputs:** a new `reconciliation` folder in config; `Reconciliation/<date>/Daily_Report_<date>` Google Sheet and .xlsx (formulas for Amount and Total; Vendor Reconciliation tab with rate + formulas; Issued detail tab). Re-run replaces both files for that date (move the old pair aside, never delete – CLAUDE.md), logged.
- **Live Daily Report tab** rewritten every cycle like the other control-sheet tabs.
- **Balance check:** computed in code and as a sheet check row; mismatch → E402 event (alert in Phase E).
- **xlsx** built with the same structure and formulas as the Google Sheet.

## Testing Decisions

- Pure tests for report rows: mixed outcomes, two amounts in one state, cancelled issue excluded, range totals.
- Cycle tests: vendor folders created; upload in a vendor folder stamps vendor on its papers; root file → E101, not stocked.
- Report command test with the in-memory Drive: Sheet + xlsx created in Reconciliation/<date>, link printed, re-run replaces (old moved aside), formulas present in xlsx.
- Balance check: a forced imbalance raises E402.

## Out of Scope

- Vendor price lists or computed costs (D6: ops type the rate).
- Email delivery of the report (Phase E alerts only).
- Fixed day-close time (D5: none).

## Further Notes

- Existing papers from the pilot have no vendor; they show "unknown" in the reconciliation.
