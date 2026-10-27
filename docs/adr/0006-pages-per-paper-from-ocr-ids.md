---
status: accepted
---

# Pages per paper are decided from the OCR'd IDs, not the text layer (supersedes part of ADR-0003)

ADR-0003 decided the paper type from the PDF text layer (text = digital, 1 page; image = traditional, 2 pages).
Real samples (2026-10-27) showed that **real e-stamps arrive as image PDFs with no text layer**, and that some
states' non-e-stamp papers are a single page (Haryana). So the text-layer rule would have sent every real e-stamp
to Needs Review. Decided with Vaibhav:

Every page is OCR'd first. The first page with exactly one ID decides the upload: an e-stamp ID (`IN-…` certificate
or `SUBIN-…` Unique Doc. Reference) → 1 page per paper; any other ID → its state's `pages_per_paper` in
`state_config.csv` (default 2 = front + back; HR = 1). Every other page with an ID must imply the same, else the
whole upload goes to Needs Review (E105); so does an upload with no readable ID at all. The rest of ADR-0003 stands:
odd page count for 2-page papers → E106 before the split API is called, and the pairing rule (E107).

## Consequences

- An unreadable page 1 (OCR error) means the type can't be decided: the whole upload is retried next cycle.
- `pages_per_paper` must be set for any state whose non-e-stamp papers are single pages (only HR is known so far).
