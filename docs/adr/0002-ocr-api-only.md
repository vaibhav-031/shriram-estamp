---
status: accepted
---

# Papers are read only by the in-house OCR API, with no fallback

Paper values (Paper ID, state, stamp duty) are read only from the in-house OCR API, at intake and at the issue
check. Google Drive OCR and the PDF text layer are removed from the read path, and there is no silent fallback: an
API failure is logged, retried, and after the retry window the upload goes to Needs Review with an alert. The text
layer is still used for one thing, deciding the paper type (ADR-0003). Reason: one reader means one set of
behaviours to test and audit, and a fallback would hide outages while issuing papers read by a different engine.

## Considered Options

- Keep the Drive OCR fallback: rejected, because it hides API outages and mixes two readers in the audit trail.
- Keep the text layer for digital e-stamps (instant and exact): rejected, so that both paper types follow one path.
