# 06: Split API adapter

Status: needs-triage

**What to build:** uploads are split by the in-house split API instead of pypdf; the adapter returns
(page_from, page_to, bytes) parts. In this phase it is called with `splitSize=1`; Phase B chooses the size per
paper type.

**Blocked by:** 03, 04

- [ ] Request matches the documented contract (multipart, ranges, `splitSize`; auth only if ticket 03 found it needed)
- [ ] Response decoded per the saved sample into ordered page ranges + PDF bytes; page numbers checked to cover the whole file with no gaps or overlaps
- [ ] Tests replay the saved split response; failure paths (401, 500, timeout, malformed body, missing pages) raise clear errors
- [ ] A full cycle with fake split + fake OCR stocks a merged upload correctly

## Comments
- 2026-10-27 Vaibhav: deferred – not on the approved shortlist (see HANDOFF.md → Current status).
