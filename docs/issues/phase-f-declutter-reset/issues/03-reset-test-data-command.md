# 03: `reset-test-data` command

Status: needs-info

**What to build:** `python -m estamp reset-test-data` lists what it will clear and move, asks the user to type YES,
then clears test ledger rows in one transaction and moves test files to a dated "Test archive" folder. Never
deletes Drive files; safe to re-run.

**Blocked by:** Phase E complete

**Needs info:** which rows are "test" (cut-off date? source files?), and whether the Karnataka sample data that was
kept on purpose is cleared at go-live.

- [ ] Nothing happens without YES (test)
- [ ] Listing matches what is then done (test against `estamp_test` + in-memory Drive)
- [ ] Run on the real ledger only with Vaibhav present, once, before go-live

## Comments

- 2026-10-27 Vaibhav: stays needs-info; he will confirm which rows are test data and the Karnataka question.
