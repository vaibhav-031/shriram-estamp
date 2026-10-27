# 09: Remove Drive OCR and text-layer reading

Status: ready-for-agent

**What to build:** the only paper reader left is the OCR API adapter (ADR-0002). Drive OCR, its temporary Google
Doc (the only Drive delete in the code), text-layer reading and their config keys are gone; `check-setup` reports
the OCR and split API settings instead.

**Blocked by:** 05, 08

- [ ] No code path reads paper values from the PDF text layer or Drive OCR; a helper that only measures whether a text layer exists may stay for Phase B
- [ ] `ocr_provider`, `ocr_fallback_to_drive`, `use_pdf_text`, `min_pdf_text_chars` (if unused) and `folders.ocr_temp` removed from config and docs
- [ ] CLAUDE.md note about the temporary OCR Doc delete updated (no Drive deletes left)
- [ ] `check-setup` shows OCR/split URL set + key present (never the key value)
- [ ] Tests that relied on the text layer now use the fake OCR client; suite green 5×

## Comments
