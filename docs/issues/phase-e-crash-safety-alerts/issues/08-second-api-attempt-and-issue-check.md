# 08: Automatic second API attempt; issue check accepts ops or API Paper ID

Status: ready-for-agent

**What to build:** any paper that fails the checks is read by the API once more before going to Needs Review. At
issue, the issue check passes when the issued PDF shows the ops-corrected Paper ID or the API's original reading for
that paper, and fails on anything else (ADR-0004).

**Blocked by:** 03, 06

- [ ] Tests: first read fails, second passes → stocked; both fail → Needs Review with both readings kept
- [ ] Tests: corrected paper passes the issue check with either reading; a third ID → mismatch (re-map rules apply)

## Comments
