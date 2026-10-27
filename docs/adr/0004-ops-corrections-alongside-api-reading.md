---
status: accepted
---

# Ops corrections are stored next to the API reading, and both pass the issue check

The OCR API reads every paper first; ops may correct the Paper ID or stamp duty in Review Actions (APPROVE with
edits), with the same checks as intake. Every correction keeps the API value, the ops value, who and when, and
the raw API response is stored per paper. At issue, the issue check accepts **either** the ops-corrected Paper ID
**or** the API's original reading for that paper; anything else is a mismatch. Reason: the OCR engine that misread
the paper at intake will usually misread it the same way at issue, so demanding the corrected ID would fail every
corrected paper, while accepting any ID would defeat the check (HANDOFF D11).
