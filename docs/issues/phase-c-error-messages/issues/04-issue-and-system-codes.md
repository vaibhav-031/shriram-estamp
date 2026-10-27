# 04: Issue, output and system problems use codes E301–E506

Status: ready-for-agent

**What to build:** issue, output sheet and system problems are reported with their codes: E301, E304, E305, E401,
E501, E502, E503, E506 now; E302/E303/E306 (Phase E), E402 (Phase D) and E505 (Drive storage low) are wired when
their behaviour exists.

**Blocked by:** 01

- [ ] Each code wired now is produced by at least one test with LAN / sheet context
- [ ] E501 (sign-in expired), E502 (database down) stop the cycle cleanly with one line; E503 waits and resumes
- [ ] E506 config error names the missing key/folder
- [ ] A test lists every catalogue code and fails if one has no producing test, except an explicit "pending phase" list

## Comments
