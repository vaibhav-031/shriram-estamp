# 04: Prefactor – OCR and split clients injected into the run context

Status: ready-for-agent

**What to build:** reading and splitting go through an OCR client and a split client held by the run context (like
the Google client), with fakes for tests. Behaviour is unchanged in this ticket: the default clients still do what
the code does today.

**Blocked by:** None (can start immediately)

- [ ] Intake and the issue check get their reader/splitter from the run context, not from module-level calls
- [ ] A fake OCR client and fake split client exist for tests (in-memory, scripted responses and failures)
- [ ] All existing tests pass unchanged, 5× in a row

## Comments
