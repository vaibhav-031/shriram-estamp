# Phase G – Production-readiness audit

Status: ready-for-agent

Source: HANDOFF.md §9.

## Problem Statement

Before go-live, money and legal documents depend on the service behaving correctly under failures nobody has
tried yet. There is no single written assessment of what could still go wrong and how bad it would be.

## Solution

A written audit, `docs/AUDIT.md`, grading every finding Critical / High / Medium / Low with file:line, a
reproduction (ideally a failing test) and a proposed fix. Critical and High findings are fixed only after Vaibhav
approves the report.

## User Stories

1. As Vaibhav, I want a report before any fix, so that I decide what is fixed before go-live.
2. As the tech team, I want each finding reproducible (ideally a failing test), so that the fix is provably right.
3. As the tech team, I want coverage of: idempotency at every step; concurrency (process lock, threads, folder and sheet creation races); API failures (OCR/split/Google: 401/403/429/5xx, timeouts, malformed JSON); token expiry; network loss mid upload/download; DB loss mid transaction; data edge cases (huge/empty Excel, extra tabs, formulas, merged cells, LAN spaces/lowercase/leading zeros, 0-page/password PDFs, rotated scans, multi-page e-stamps); money correctness (reports always balance, cancellations reflected); security (secrets, no PII in logs/DB, token file permissions); observability (every failure → code + Log row); performance at 10.8k/day with the 5 rps limit; config errors.

## Implementation Decisions

- Audit first, fixes second (separate tickets created from the approved findings).
- Use the built-in `code-review` at max effort only if Vaibhav asks for it; otherwise high.

## Testing Decisions

- Each Critical/High finding gets a failing test before its fix.

## Out of Scope

- Medium/Low fixes unless approved.
