# 01: Detect paper type per upload; mixed/unclear → Needs Review

Status: ready-for-agent

**What to build:** before splitting, each upload is classed digital (every page has a real text layer), traditional
(every page is image only) or unclear/mixed. Unclear/mixed uploads go whole to Needs Review with reason E105.

**Blocked by:** Phase A complete

- [ ] Pure detection function with threshold from config; unit tests for all-digital, all-scanned, mixed, near-threshold
- [ ] Cycle test: mixed upload moves whole to Needs Review, no paper stocked, reason names E105
- [ ] Text layer never used to read values (ADR-0002)
- [ ] Re-running the cycle does not process the file again

## Comments
- 2026-10-27: done. Pure paper_type() in extract.py; threshold = intake.min_pdf_text_chars (one threshold for type + reader). Mixed → whole upload REVIEW (E105), nothing split. A page with an unreadable text layer counts as 0 chars (→ review, not a retry loop).
