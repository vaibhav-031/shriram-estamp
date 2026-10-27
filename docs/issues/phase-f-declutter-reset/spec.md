# Phase F – Declutter Drive and reset test data

Status: ready-for-agent

Source: HANDOFF.md §8.

## Problem Statement

The Drive area mixes ops folders, system folders and pilot leftovers (old samples, an old Apps Script folder, test
order files, unused control-sheet tabs). Ops can't tell which folders are theirs, and test rows in the ledger would
pollute go-live stock and reports.

## Solution

A numbered layout where ops see only their five folders and the control sheet, and everything the system owns sits
under "⚙️ System (don't touch)". Folders are referenced by ID, so moving them is safe and `check-setup` proves it.
Old items are removed only after Vaibhav confirms each one. A `reset-test-data` command (typed YES) clears test
ledger rows and moves test files to a dated Test archive folder, once, before go-live.

## User Stories

1. As ops, I want to see only Upload Inbox (with vendor folders), Orders Incoming, Output, Needs Review, Reconciliation and the control sheet, numbered in the order I use them.
2. As the tech team, I want the system folders grouped under "⚙️ System (don't touch)".
3. As the tech team, I want `check-setup` to prove every folder ID still resolves after the moves.
4. As Vaibhav, I want each old item (Old samples (wrong amounts), TEST_Shriram_Order_Summary_26Oct, Shriram eStamp (Test) + its control sheet, tabs Sheet1 and Dry Run Preview) removed only after I confirm it.
5. As Vaibhav, I want `python -m estamp reset-test-data` to ask me to type YES, clear test ledger rows and move test files to a dated Test archive, so that go-live starts clean.
6. As the tech team, I want the reset to never delete Drive files and to print exactly what it will do before asking for YES.

## Implementation Decisions

- **Layout moves** are done on the real Drive, so each move needs Vaibhav's go-ahead; config keeps folder IDs, so no config change is needed for moves, only for the new Reconciliation folder (Phase D).
- **"Remove"** means move to an archive folder (CLAUDE.md: never delete user files); Dry Run Preview tab handling decided with Vaibhav (the dry-run feature still writes it).
- **reset-test-data:** a dry listing first (counts per table, files to move), then the YES prompt; runs in one transaction for the ledger; moves files, never deletes. Which rows count as "test" is defined explicitly in the ticket (e.g. everything before a cut-off date, or by source file) – to be agreed with Vaibhav.

## Testing Decisions

- reset-test-data tested against `estamp_test` + in-memory Drive: listing matches, nothing happens without YES, ledger cleared, files moved, nothing deleted, safe to re-run.
- Layout: `check-setup` test with moved folders in the in-memory Drive.

## Out of Scope

- Moving to the Shared Drive (Phase H).

## Further Notes

- **Open:** the saved note says the Karnataka sample papers and LAN sheet in the pilot ledger are test data to leave alone for now. The reset ticket must confirm with Vaibhav whether they are cleared at go-live.
