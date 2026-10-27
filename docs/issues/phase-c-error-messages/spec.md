# Phase C – Clear error messages for ops

Status: ready-for-agent

Source: HANDOFF.md §5 and §12 (terminal + `explain`).

## Problem Statement

Ops watch a terminal and a Log tab full of free-text messages and stack traces. The same problem is worded
differently in different places, and most messages don't say what the system already did or what ops should do
next. Ops can't tell "nothing to do" from "call tech now".

## Solution

One error catalogue: every problem ops can hit has a code (E101–E506), one message template with context
(file, page, LAN), what the system did and what ops should do. Every event prints one clear line in the terminal
and the Log tab, successes print `✓` lines, stack traces go only to the log file, and each cycle ends with a
one-screen summary. `python -m estamp explain E111` prints any code's meaning and action.

## User Stories

1. As ops, I want every problem shown as one `✗ [E###]` line with the file/page/LAN it's about, so that I know exactly what failed.
2. As ops, I want each line to say what the system did, so that I know whether it will retry by itself.
3. As ops, I want each line to say what I should do (often "nothing"), so that I don't guess.
4. As ops, I want `✓` lines for successes, so that I can see work happening.
5. As ops, I want a one-screen summary at the end of every cycle (received / stocked / in review / issued / waiting / errors), so that I can glance at progress.
6. As ops, I never want to see a stack trace on screen; the tech team gets it in the log file.
7. As ops, I want `python -m estamp explain E111` to print the meaning and my action for any code.
8. As ops, I want the Log tab to show the same code and wording as the terminal.
9. As the tech team, I want all wording in one catalogue, so that changing a message is one edit.
10. As the tech team, I want a test proving every code is produced by at least one scenario, so that the catalogue never drifts from behaviour.

## Implementation Decisions

- **Catalogue module:** code → title, message template (named placeholders), "what I did", "what you do", severity (info / warn / error), major-alert flag (used in Phase E alerts). Codes and wording exactly as HANDOFF §5 table.
- **One event API:** emitting an event takes a code + context values; it formats the terminal line, writes the events table (code stored in its own column) and the Log tab. Existing free-text events are migrated to codes; a small set of plain INFO progress lines may stay uncoded.
- **Terminal output:** console handler prints the formatted three-line block for errors/warnings and one line for `✓`/info; the rotating log file gets full detail including tracebacks.
- **Codes whose behaviour arrives later** (E101 vendor folders – Phase D; E302/E303 re-map, E306 self-heal – Phase E; E402 reconciliation – Phase D; E505 storage low) are catalogued now and wired in their phase; the "every code has a test" check lists them as pending until then.
- **E504** "Another run already active" replaces today's plain log warning.
- **`explain` command** added to the CLI; unknown code → list of valid codes.

## Testing Decisions

- Catalogue tests: every code has all fields; templates render with their placeholders; `explain` output for a known and an unknown code.
- Scenario tests through the cycle seam: each code is produced by at least one test, and its message contains the file/page/LAN context.
- Console test: an exception inside a step prints the coded line but no traceback; the log file has the traceback.
- Prior art: existing cycle tests already produce most situations – extend their assertions rather than adding parallel tests.

## Out of Scope

- Email alerting and dedup (Phase E, §12).
- New behaviours behind codes (vendor folders, re-map, reconciliation) – their own phases.

## Further Notes

- Events table gains a code column; old rows without a code stay readable.
