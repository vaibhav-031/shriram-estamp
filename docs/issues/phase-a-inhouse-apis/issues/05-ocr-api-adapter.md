# 05: OCR API adapter used at intake and at the issue check

Status: ready-for-agent

**What to build:** papers are read by the in-house OCR API at intake and at the issue check, using the real
contract from ticket 03, and the raw API response is stored per paper.

**Blocked by:** 03, 04

- [ ] Request matches the documented contract (headers, JSON body, base64 PDF, `ocr_config_id` from config); key and org account from `.env` only
- [ ] Response mapped to Paper ID / Unique Doc. Reference / state / stamp duty / text; structured fields preferred, existing parsers used on the text otherwise
- [ ] Raw response stored per paper in the ledger (schema change safe to re-run)
- [ ] The issue check uses the same adapter
- [ ] Tests replay the saved responses through a full cycle: a digital and a traditional paper are stocked and issued
- [ ] Adapter tests for 401, 403, 500, timeout, garbage JSON, empty result each raise a distinct, clear error

## Comments
