# 07: Shared rate limiter, Retry-After and backoff for both APIs

Status: ready-for-agent

**What to build:** the service never sends more than the configured requests per second to the OCR API (default 5)
across all threads, slows down on 429, and retries 5xx/timeouts with exponential backoff. The split API has its own
limiter.

**Blocked by:** 05, 06

- [ ] Rate and retry counts in config
- [ ] Test: 50 parallel requests, no 1-second window has more than 5 OCR calls
- [ ] Test: 429 with Retry-After waits at least that long; 5xx and timeouts back off and retry up to the limit
- [ ] Test: 401/403 is never retried
- [ ] The issue check shares the OCR limiter with intake

## Comments
- 2026-10-27: done. estamp/http_api.py: sliding-window limiter per API+rate shared by all threads; 429 pauses the shared limiter (Retry-After capped at 60 s); 5xx/timeout/connection error retried with backoff up to retries and a max_total_s cap; 401/403 → ApiAuthError with API name + server message, never retried; retries logged. Split and OCR adapters both go through it. 50-call fake-clock test proves ≤5 per 1-s window; 100-thread test proves sharing. Code-review: 10 findings, all fixed. Log-tab events (ctx.event) for retries come with Phase C codes (E112).
