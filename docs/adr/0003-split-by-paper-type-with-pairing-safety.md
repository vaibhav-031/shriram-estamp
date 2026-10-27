---
status: accepted (type detection superseded by ADR-0006; odd-page and pairing rules stand)
---

# Split by paper type; a broken pairing stops the rest of the upload

Each upload's paper type is decided before splitting: a real text layer means digital e-stamp (1 page per paper);
scanned images mean traditional paper (always 2 pages, front + back, confirmed 2026-10-27). Mixed or unclear
uploads go whole to Needs Review (E105), and a traditional upload with an odd page count goes whole to Needs Review
(E106). This replaces the pilot's content-based page grouping.

After split and OCR, every traditional pair must have exactly one serial, on the front page. At the first pair that
fails, that pair **and every later pair of the same upload** go to Needs Review (E107), because one missing back
page shifts every later pair onto the wrong front. Earlier good pairs are stocked. Ops fix it by uploading the
affected papers again as separate 2-page PDFs and rejecting the broken entries.
