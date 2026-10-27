# 01: Volume test – ~1,000 LANs, zero double issue

Status: ready-for-agent

**What to build:** a test in the normal suite that stocks papers and LANs across several states and stamp duties
(~1,000 LANs), runs cycles until done, and proves every LAN got exactly one paper and no paper was issued twice.
It guards every later phase (definition of done).

**Blocked by:** None (can start immediately)

- [ ] ~1,000 LANs over at least 4 state + stamp duty groups, including one group with fewer papers than LANs (shortage)
- [ ] Asserts: each issued LAN has exactly one live issue; each paper is issued at most once; shortage LANs stay PENDING
- [ ] Uses the in-memory Drive and `estamp_test` only; runs in every suite run and keeps the 5× suite practical (record its run time in the ticket)
- [ ] HANDOFF.md definition of done says "~1,000 LANs" instead of "10.8k"

## Comments
