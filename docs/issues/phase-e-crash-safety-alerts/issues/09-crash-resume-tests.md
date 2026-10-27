# 09: Prove resume after a crash at every step

Status: ready-for-agent

**What to build:** tests that inject a crash at each step of intake and issue (before/after backup, after move,
after the issue check, during the output sheet write, mid-intake) and prove the next cycle ends in the correct state
with nothing duplicated or lost. Any gap found is fixed in this ticket (diagnosing-bugs first).

**Blocked by:** 01, 02, 03

- [ ] One test per crash point, listed in the ticket Comments with its outcome
- [ ] Volume test still green

## Comments
