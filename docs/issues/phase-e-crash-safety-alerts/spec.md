# Phase E – Crash safety, ops corrections and alerts

Status: ready-for-agent

Source: HANDOFF.md §7 (E-1…E-7), §12, decisions D7, D8, D11; ADR-0001, ADR-0004, ADR-0005; grilling 2026-10-27
(G1: D7 + E-1 confirmed; G5: Gmail + heartbeat URL).

## Problem Statement

After a laptop crash, LANs caught mid-issue sit in ERROR until ops type RETRY, even when Drive clearly shows
where the file is. A failed issue check parks the LAN for a human even though the fix is almost always "use the next
paper". A crash between writing an output row and recording it can put the same LAN in Shriram's sheet twice. Files
copied into Inventory by hand are invisible. When the service is down, ops have no recorded way to issue by hand.
When the API misreads a paper, ops can only approve it with an edit and no audit trail, can't ask the API to try
again, and the issue check would then reject the corrected paper. Alerts go to Slack/SMTP, deduplicated per day,
with no clear split between "major" and "ops handle it".

## Solution

Two allowed automatic actions (ADR-0001): self-heal stuck issues from Drive evidence, and re-map once after a failed
issue check. Output rows are never appended twice. Stray Inventory files are adopted (never deleted). A Manual
Issues tab records hand issues with the same checks. Review Actions gain audited corrections, RE-OCR and RECHECK; an
automatic second API attempt happens before any paper lands in Needs Review; the issue check accepts the ops ID or
the API's original reading (ADR-0004). Crash-at-every-step tests prove resume. Major issues are emailed through the
Gmail API with hourly dedup and digests; the external heartbeat stays (ADR-0005).

## User Stories

1. As ops, after a crash I want stuck LANs resolved automatically from where their file really is, so that I don't RETRY by hand.
2. As ops, I want a LAN whose file is neither in Inventory nor in Output put in ERROR with an alert, so that real problems still reach a human.
3. As ops, I want a failed issue check to quarantine the paper and give the LAN the next paper once, so that one bad paper doesn't block a LAN.
4. As ops, I want a second failed issue check on the same LAN to put it in ERROR (E303), so that repeated failures get a human.
5. As Shriram, I never want the same LAN twice in an output sheet.
6. As ops, I want PDFs in Inventory that the ledger doesn't know about checked and adopted (or sent to Needs Review), never deleted, with a Log line.
7. As ops, when the system is down and I issue a paper by hand, I want to record it in a Manual Issues tab and have the service validate and record it like a normal issue, never double-issuing.
8. As ops, I want Needs Review to show exactly what the API read for each paper.
9. As ops, I want to APPROVE a paper with a corrected Paper ID and/or stamp duty, with the same checks as intake.
10. As ops, I want RE-OCR to send a paper to the API again, and RECHECK to re-validate the saved API result after a config fix, per paper.
11. As the ops manager, I want every correction audited (API value, ops value, who, when) and visible in a Corrections tab, including which LAN the paper later went to.
12. As ops, I want the API asked a second time automatically before a paper goes to Needs Review, so that one-off misreads fix themselves.
13. As ops, I want a corrected paper to pass the issue check whether the API reads the corrected ID or its original reading.
14. As the tech team, I want tests that crash at every step of intake and issue and prove the next run ends correct with nothing duplicated or lost.
15. As Atul and Vaibhav, I want an email for each major issue (E113, E110/E111 for 30+ min, E126, E302/E303, E304/E305, E402, E501/E502, E505/E506, first daily E301 per state + amount, CRASH, HEARTBEAT).
16. As Atul and Vaibhav, I want the same code + object emailed at most once per hour, and several events in one cycle combined into one digest.
17. As Atul and Vaibhav, I want a daily summary email at a configurable hour (default 20:00 IST).
18. As the tech team, I want emails sent through the Gmail API from the signed-in account, never containing keys, customer names or full OCR text.
19. As the tech team, I want the external heartbeat URL kept, so that a dead or sleeping laptop is still noticed.

## Implementation Decisions

- **Self-heal (E-1):** at cycle start, LANs in ALLOCATING / papers RESERVED older than N minutes are resolved by Drive location: in Inventory → release (paper AVAILABLE, LAN PENDING); in Output as `<LAN>.pdf` → finish (backup if missing, issue check, ledger row, DONE); else ERROR + alert. Each decision logged as E306. Replaces "stuck → ERROR". CLAUDE.md safety rule and the cycle/issue docstrings amended to point at ADR-0001.
- **Re-map (D7):** on issue-check mismatch the issued PDF moves to Needs Review/<date>/Cert mismatch, paper → quarantined status, LAN gets the next paper in the same cycle; re-map count stored per LAN; second mismatch → LAN ERROR (E303). E302 logged on the first.
- **Output dedup (E-3):** before appending, read the target sheet's LAN column; skip LANs already present (as ISSUED), then mark written.
- **Stray files (E-2):** every N cycles, list Inventory recursively; unknown PDF → OCR + checks → AVAILABLE (vendor "unknown") or Needs Review; "stray file adopted" in Log. Never delete.
- **Manual Issues tab (E-4):** `LAN | Paper ID | Issued by | Result | Processed At`; validates LAN PENDING, paper AVAILABLE, same state + stamp duty; records an issue exactly like an automatic one (ledger, output row) without moving files ops already moved; never double-issues. Service fills only Result/Processed At.
- **Review Actions (D11, ADR-0004):** header becomes `Paper ID | Action | Paper ID (corrected) | Stamp Duty (corrected) | Result | Processed At` with actions APPROVE / RE-OCR / RECHECK / REJECT (state is derived from the Paper ID). Paper gains audit fields: API Paper ID, API stamp duty, ops Paper ID, ops stamp duty, edited by, edited at. "Edited by" = the control sheet's last editor if the Sheets/Drive API exposes it, otherwise a required "Your name" column – decided in ticket 06. Corrections tab lists every edit with the LAN it was later issued to.
- **Second API attempt:** any paper failing checks is read once more by the API before Needs Review; both raw responses kept.
- **Issue check:** passes when the IDs found equal {ops Paper ID} or {API Paper ID} for that paper.
- **Alerts (ADR-0005):** add `gmail.send` scope (one re-login, documented); `alerts.email_to` config (atul.kumar@zoop.one, vaibhav.sharma@zoop.one); major flag from the Phase C catalogue; dedup table keyed by code + object + hour; one digest per cycle; subject `[Shriram eStamp] <code> <title>`; body = message, what the system did, what to do, control-sheet link. Slack and SMTP code and config removed. Heartbeat URL ping kept. HEARTBEAT email = no successful cycle for 30 min while `run` is active (best effort). CRASH email with a log excerpt scrubbed of secrets/PII.

## Testing Decisions

- All behaviour through the cycle seam with the in-memory Drive, fake OCR client and `estamp_test`.
- Self-heal: one test per Drive location (Inventory / Output / missing), plus "finish" re-runs the issue check.
- Re-map: mismatch then success; mismatch twice → ERROR; quarantined paper never issued again.
- Output dedup: crash between append and mark-written → re-run writes no duplicate row.
- Stray files: unknown valid PDF adopted; unknown invalid PDF → Needs Review; nothing deleted.
- Manual Issues: valid row recorded; wrong state/amount, paper not AVAILABLE, LAN already issued → rejected with reason; re-run doesn't double-issue.
- Review: APPROVE with edits runs intake checks and writes audit fields; RE-OCR calls the API again; RECHECK makes no API call; issue check accepts ops or API ID and rejects anything else.
- Crash-resume (E-6): a fault injected at each step (before/after backup, after move, after issue check, during sheet write, mid-intake) → next cycle ends correct, nothing duplicated or lost. Prior art: `test_crash_recovery_and_dry_run`.
- Alerts with a mocked Gmail client: routing major vs not, hourly dedup, digest, no secrets/PII in body, daily summary once per day.

## Out of Scope

- Auto-start after reboot (D8: ops re-run the command).
- Changing the external heartbeat provider.

## Further Notes

- The Review Actions header change breaks any rows ops typed under the old header: ticket 06 must migrate or clearly reset that tab (Vaibhav to confirm at that point).
