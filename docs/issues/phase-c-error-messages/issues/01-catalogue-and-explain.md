# 01: Error catalogue, coded events and `explain`

Status: ready-for-agent

**What to build:** a single catalogue of every code in HANDOFF §5 (message, what the system did, what ops do,
severity, major flag), an event call that takes a code + context and prints the three-line `✗ [E###]` block in the
terminal and the Log tab, and `python -m estamp explain <code>`. Proven end-to-end with one real code (E504
"another run already active").

**Blocked by:** Phase B complete

- [ ] Catalogue holds every HANDOFF §5 code with exact wording
- [ ] Events table stores the code; Log tab shows it
- [ ] E504 emitted when the run lock is held (test)
- [ ] `explain E111` prints meaning + ops action; unknown code lists valid codes
- [ ] Stack traces only in `logs/estamp.log`, never on screen (test)

## Comments
