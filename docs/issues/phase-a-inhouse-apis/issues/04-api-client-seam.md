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
- 2026-10-27: done. Open review points for the dev team: intake still reads `intake.split_provider` to decide grouping instead of asking the splitter; fakes can script failures but not OCR values; FakeOcr failure keys match by substring (`_p2` also hits `_p20`); issue check still splits with the private pypdf helper; new Ctx fields sit before `resolver` (keyword use only); ConfiguredOcr captures `google` at construction.
