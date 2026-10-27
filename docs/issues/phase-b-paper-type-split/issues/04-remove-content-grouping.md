# 04: Remove content-based page grouping; ops instructions for broken pairing

Status: ready-for-agent

**What to build:** the old "group pages by what's printed on them" logic is gone; tests that still describe valid
behaviour are kept and rewritten for the new rules; README tells ops how to fix E107.

**Blocked by:** 03

- [ ] No grouping-by-content code path remains; unused marker config removed or its remaining use documented
- [ ] Surviving cases from the old front/back and e-stamp-grouping tests rewritten, not dropped
- [ ] README ops section: re-upload affected papers as separate 2-page PDFs into the vendor folder, then REJECT the broken entries
- [ ] Suite green 5×, volume test included

## Comments
