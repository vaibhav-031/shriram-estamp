# 03: Re-map once after a failed issue check (D7, ADR-0001)

Status: ready-for-agent

**What to build:** when the issue check fails, the issued PDF moves to Needs Review/<date>/Cert mismatch, the paper
is quarantined, and the same LAN gets the next Inventory paper (E302). A second failure on the same LAN puts it in
ERROR (E303).

**Blocked by:** Phase D complete

- [ ] Re-map count stored per LAN; survives restarts
- [ ] Quarantined paper never issued again without a human
- [ ] Tests: mismatch then success; mismatch twice → ERROR; no stock left after mismatch → LAN back to PENDING (shortage), not ERROR
- [ ] Ops README describes what E302/E303 mean and what to check

## Comments
