# Shriram eStamp – project rules

This is a **live e-stamp fulfilment service** for Shriram Finance (Python 3.15, Postgres via Postgres.app on
port 3000, Google Drive + Sheets). Vendor stamp papers are matched to loan accounts (LANs) and issued as PDFs.
Money and legal documents are involved: **correctness over speed**. A wrong or duplicated certificate issued to
a LAN is a real-world legal problem, not a cosmetic bug.

See `README.md` for the flow and `estamp/cycle.py` for the order of steps in one cycle.

## Safety rules

- **Drive: never delete user files.** Move / rename / copy only. There is no delete anywhere in the code
  (the old temporary-OCR-doc delete in `google_clients.ocr_text` went away with Drive OCR in A-09 – keep it that way).
- **Drive boundary: only the Shared Drive "Shriram Finance eStamps" (`0AECjrUF6gCffUk9PVA`).** Live runs sign in as
  procurement@zoop.one; every live folder ID must sit inside this Shared Drive, and nothing outside it (any My Drive,
  any other Shared Drive) is ever created, moved, renamed or edited.
- **Never touch the real Drive folders or the real database without asking first.** That includes
  `python -m estamp run-once|run|recheck-review|reconcile`, any write to schema `shriram_estamp`, and any script that
  uses the folder IDs in `config.yaml`. `config.yaml` has `dry_run: false` – a run is LIVE.
- For development use `tests/fake_google.py` (in-memory Drive/Sheets) and the separate test schema `estamp_test`.
- **Secrets live only in `.env` and `secrets/`.** Never read them into chat, never print, log or commit them.
  Don't `cat`/`source` `.env` in a visible command; don't echo `ESTAMP_DB_URL`. Both are in `.gitignore`.

## Workflow for every change

1. One phase at a time. **Stop for approval between phases.**
2. Any failure or unexpected behaviour → **`diagnosing-bugs` skill first**, before changing code.
3. **Tests first** (`tdd` skill): write or extend a failing test, then change code.
4. Run the **full suite 5 times in a row**; all 5 must pass:
   ```bash
   source .venv/bin/activate
   for i in 1 2 3 4 5; do ESTAMP_TEST_DB_URL=postgresql://vaibhav.sharma@localhost:3000/postgres \
     python -m pytest -q tests/ || break; done
   ```
   (Passwordless local Postgres.app; the e2e tests create and drop only the `estamp_test` schema.)
5. **`code-review` skill** (the built-in one: correctness bugs, verified findings) on the change before
   committing. When the change implements a spec/ticket in `docs/issues/`, also run **`code-review-mp`**
   (Matt Pocock's: repo standards + spec conformance).
6. Commit with a clear message. Destructive git commands (push, reset --hard, clean, branch -D, restore .) are
   blocked by `.claude/hooks/block-dangerous-git.sh`.

## Code conventions

- Match the surrounding style: short module docstrings explaining the flow, dense comments only where a rule is
  non-obvious, `ctx.event(...)` for anything ops should see in the Log tab.
- Pure parsing/checking logic lives in `estamp/extract.py` (no Google, no DB) – keep it that way so it stays
  unit-testable.
- Tunables (patterns, markers, amounts) belong in `config.yaml` / `state_config.csv`, not hard-coded.
- Every step of a cycle must be safe to re-run; anything uncertain goes to ERROR / Needs Review for a human,
  never silently retried or reassigned – with exactly two logged exceptions (`docs/adr/0001-limited-automatic-recovery.md`):
  **re-map once** after a failed issue check (second failure on the same LAN → ERROR), and **self-heal stuck issues**
  from the file's real Drive location (anywhere unexpected → ERROR). Add no other automatic retry/reassign without a new ADR.

## Agent skills

### Issue tracker

Local markdown files under `docs/issues/`. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`).
See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` at the repo root + `docs/adr/`. See `docs/agents/domain.md`.
