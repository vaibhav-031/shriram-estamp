# 07: RE-OCR and RECHECK review actions

Status: ready-for-agent

**What to build:** in Review Actions, RE-OCR sends the paper to the API again and re-runs the checks; RECHECK
re-validates the stored API result after a config fix, with no API call. Passing papers move into Inventory.

**Blocked by:** 06

- [ ] RE-OCR stores the new raw response alongside the old one
- [ ] RECHECK makes no API call (test asserts the fake OCR client was not called)
- [ ] The `recheck-review` command uses the same logic for all Needs Review papers
- [ ] README review-action docs updated

## Comments
