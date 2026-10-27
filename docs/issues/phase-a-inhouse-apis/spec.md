# Phase A – In-house OCR and split APIs

Status: ready-for-agent

Source: HANDOFF.md §3, decisions D1, D2, D10; ADR-0002; grilling 2026-10-27 (G4, G6, G7).

## Problem Statement

Ops need every paper read the same way, by the company's own OCR API, at intake and at the issue check. Today the
service reads the PDF text layer first, falls back to Google Drive OCR, and the in-house API code is a stub written
against a guessed contract that does not match the real one. There is no rate limit, so a busy day could flood the
API, and an API outage would either fail silently into Drive OCR or leave uploads stuck without a clear signal.

## Solution

One OCR adapter and one split adapter, built against the real dev endpoints and shaped by real saved responses.
All reading at intake and at the issue check goes through the OCR adapter; Drive OCR and text-layer reading are
removed. Both adapters share a process-wide rate limiter and a clear retry/failure policy. The raw API response is
kept per paper so later phases can show ops exactly what the API read.

## User Stories

1. As ops, I want every paper read by the in-house OCR API, so that stock decisions do not depend on which reader happened to run.
2. As ops, I want an API outage to leave my upload in the Upload Inbox and be retried next cycle, so that nothing is lost or half-stocked.
3. As ops, I want an upload whose OCR keeps failing for the retry window to move to Needs Review with a reason, so that it doesn't sit unnoticed forever.
4. As ops, I want intake to stop for the cycle with one loud message when the API key is rejected, so that I tell the tech team at once instead of watching hundreds of failures.
5. As the tech team, I want at most 5 OCR requests per second across all threads (configurable), so that the API is never flooded.
6. As the tech team, I want 429 responses to slow the service down using Retry-After, so that rate limiting is handled without errors.
7. As the tech team, I want 5xx responses and timeouts retried with exponential backoff, so that short blips recover on their own.
8. As the tech team, I want the split API to have its own limiter, so that splitting and reading don't starve each other.
9. As the tech team, I want the issue check to use the same OCR adapter and limiter, so that intake and issue read papers identically.
10. As the tech team, I want the raw OCR API response stored for every paper, so that Needs Review can show exactly what the API returned (Phase E).
11. As the tech team, I want the real request and response shapes saved (redacted) in the repo, so that tests mock the real contract, not a guess.
12. As the tech team, I want the API key and org account read only from `.env`, so that secrets never reach config, code, logs or commits.
13. As the tech team, I want average seconds per call, payload limits and bad-input behaviour measured, so that we know whether a 10.8k-paper day finishes in under 4 hours.
14. As Vaibhav, I want a proposal (not an implementation) for front-page-only OCR of traditional papers if the day doesn't fit in 4 hours, so that I can decide.
15. As the tech team, I want a volume test (~1,000 LANs) in every suite run proving zero double issue, so that every phase keeps that guarantee.
16. As a new developer, I want README to show the real Python version, Postgres port and `dry_run` value, so that setup doesn't mislead me into a live run.

## Implementation Decisions

- **OCR API contract (from HANDOFF, to be confirmed by saved samples):** `POST` to the dev parsely OCR endpoint with headers `x-zoop-key` and `x-zoop-org-acc`, JSON body `{"mode":"sync","ocr_config_id":…,"documents":[{"data":<base64 PDF>}]}`. URL and `ocr_config_id` in config; key and org account in `.env` (`ZOOP_OCR_API_KEY`, `ZOOP_OCR_ORG_ACC`).
- **Mapping:** prefer structured fields if the API returns them (Paper ID, Unique Doc. Reference, state, stamp duty, first party); otherwise run the existing pure parsers on the returned text. Mapping lives with the adapter; parsing stays in the pure extract module.
- **Split API contract:** multipart with `file`, `ranges[0][startPage]`, `ranges[0][endPage]`, `splitSize`. Whether auth is needed and how parts come back (zip / JSON / URLs) is learned from the saved sample. The adapter returns a list of (page_from, page_to, bytes). In Phase A it is called with `splitSize=1` so content grouping keeps working until Phase B replaces it.
- **New seam:** the OCR client and split client are injected into the run context (like the Google client), so tests swap in fakes built from the saved responses. The HTTP layer is the only thing faked.
- **Rate limiter:** one token bucket per API, shared by all worker threads in the process, rate in config. 429 honours Retry-After; 5xx/timeout → exponential backoff up to N tries (config).
- **Failure policy:** after N tries a page's read fails → whole upload stays in the Inbox, retried next cycle; first-failure time is tracked per upload in the ledger; after M minutes (config, default 30) → Needs Review with reason + alert. 401/403 → stop intake for the rest of the cycle, one error (E113 wording arrives in Phase C), no retries.
- **Schema:** papers gain a raw OCR response column (JSON); uploads gain first-failure time and failure count.
- **Removed:** Drive OCR, the temporary Google Doc delete, text-layer reading, `ocr_provider` / `ocr_fallback_to_drive` / `use_pdf_text` config keys. The text-layer *check* stays available for Phase B type detection only.
- **API samples:** saved under `docs/api-samples/`, with names and LAN-like values redacted before writing; each live call needs Vaibhav's go-ahead first (live calls are outside the test suite).
- **Docs:** README fixes (Python 3.15, port 3000, `dry_run: false` = LIVE, test command per CLAUDE.md); HANDOFF definition of done changes "10.8k" to "~1,000 LANs" (G4).

## Testing Decisions

- Test external behaviour through the highest seam: a full cycle with the in-memory Drive, the `estamp_test` schema and fake OCR/split clients replaying the saved responses. Prior art: `test_full_flow`, `test_issue_time_ocr_catches_swapped_file`.
- Adapter unit tests: mapping of the saved responses; 401, 403, 429 with Retry-After, 500, timeout, garbage JSON, empty result.
- Rate-limit test: 50 parallel requests, no 1-second window holds more than 5 calls (fake clock where possible).
- Failure-policy tests: upload stays in Inbox while failing; moves to Needs Review after the window; auth failure stops intake for the cycle and does not retry.
- Volume test: ~1,000 LANs across several states/amounts, every LAN gets exactly one paper, no paper issued twice.
- Existing tests that depend on the text layer or Drive OCR are rewritten to use the fake OCR client, not deleted.

## Out of Scope

- Paper-type detection, 2-page split, pairing rules (Phase B).
- Error-code catalogue and message wording (Phase C) – Phase A emits plain events that Phase C re-words.
- Showing the raw API output in Needs Review, RE-OCR / RECHECK actions (Phase E).
- Production URLs and the rotated key (Phase H).

## Further Notes

- The key was shared in chat and **must be rotated before go-live** (Phase H).
- Volume check: ~10.8k papers/day ≈ 25–35k OCR calls/day including the issue check; at 5 rps that is roughly 1.4–2 hours of pure calls if calls are quick, but real latency per call decides it.
