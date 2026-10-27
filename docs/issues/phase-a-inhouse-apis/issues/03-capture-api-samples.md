# 03: Capture real OCR and split API responses (redacted)

Status: ready-for-agent

**What to build:** saved, redacted request/response samples from the dev OCR and split APIs, plus a short write-up
of the response shapes, so adapters and tests are built against the real contract.

**Blocked by:** None (can start immediately)

**Safety:** every live API call needs Vaibhav's go-ahead first. Key and org account are read from `.env` only and
never printed. Samples: one digital UP e-stamp page, one traditional RJ front page (OCR); a 4-page sample (split).

- [ ] `docs/api-samples/` holds OCR responses for both samples and the split response + headers, with names and LAN-like values replaced
- [ ] A README in that folder documents: OCR response fields (structured Paper ID / state / stamp duty / party? raw text?), split response format (zip / JSON / URLs, page numbers), whether split needs auth
- [ ] Records time per call, payload size limits found, and what each API returns for bad input (non-PDF, empty, corrupt)
- [ ] Nothing secret in any committed file (grep for the key before commit)

## Comments

- 2026-10-27 Vaibhav: approved. Keep samples redacted; ask before each live call.
