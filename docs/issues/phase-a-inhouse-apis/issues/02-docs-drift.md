# 02: Fix setup docs that point at the wrong Python, port and dry-run value

Status: ready-for-agent

**What to build:** README and docstrings match reality, so nobody starts a live run thinking it is a preview.

**Blocked by:** None (can start immediately)

- [ ] README: Python 3.15, Postgres.app on port 3000, test command as in CLAUDE.md
- [ ] README: states that `config.yaml` has `dry_run: false` (LIVE) and how to preview safely (`run-once --dry-run`)
- [ ] The pure-logic module's docstring points at the real test file
- [ ] No code behaviour changes

## Comments
- 2026-10-27: done.
