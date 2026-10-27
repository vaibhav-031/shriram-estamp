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
