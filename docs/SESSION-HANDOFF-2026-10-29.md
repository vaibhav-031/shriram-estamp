# Session handoff – 2026-10-29

Context for continuing work on the Shriram eStamp service in another assistant. Read `CLAUDE.md` (binding rules),
`README.md`, `ARCHITECTURE.md`, `docs/AUDIT.md` and `docs/adr/` first.

## State of the code
- Branch `main`. Local `main` is 4 commits ahead of GitHub (`22a2241`, `740f541`, `7578ac8`, `2923072`) – push with
  `git push origin main` (Claude's git hook blocks pushes; the user pushes).
- Full suite: 292 passed (`ESTAMP_TEST_DB_URL=postgresql://vaibhav.sharma@localhost:3000/postgres uv run pytest -q tests/`).
- Production DB (self-hosted, `ESTAMP_DB_URL` in `.env`) schema is up to date (`init-db` run 2026-10-29).
- `config.yaml` has `dry_run: true`; live runs use `--live`: `caffeinate -i uv run python -m estamp run --live` (Mac),
  `uv run python -m estamp run --live` (Windows). Only ONE machine runs the loop at a time.

## Done today
- `state_config.csv` follows ops' vendor sheet "Shriram Finanace daily orders" (source of truth): amounts (MH ₹100,
  TR ₹200), daily qty = 10-day qty ÷ 10, pages per paper (only OD, WB, MP, CG, SK are front + back), all states active.
  Test `test_state_config_matches_vendor_sheet` pins it.
- Windows fixes: console ✗ logging error; `setup.ps1` ASCII-only (PowerShell 5.1 read UTF-8 as cp1252).
- Upload settle window 3 min.
- Six-engineer hardening (all of docs/AUDIT.md except M-3): error codes E110–E126 wired, encrypted/oversized PDFs
  (E103/E108, 50 MB cap), H-5 no re-billed OCR, intake deadline inside files, whole-file reviews counted, lock-loss
  stop (M-2), crashed runs closed (M-7), alerts ERROR-first (L-3), startup config check (L-1), secret files must be
  chmod 600 on Mac/Linux (H-2), Excel formula/row cap (H-3), output sheet reuse (M-1), move lock (M-5), page-text
  pruning 30 d / events 90 d (H-1), LAN canonical form = whitespace removed + uppercase (M-6, ADR-0008).
- Review Actions: column A takes a Paper ID **or a file name** (whole-file review, e.g. `page62.pdf`). Any APPROVE
  with a typed Cert No stores the paper with `ocr_via='ops'` and allocation **trusts the typed cert instead of the
  second OCR** (stamps over numbers make re-reads fail). Typed physical serial → `STATE-SERIAL` (spaces dropped).
- E107 fix: on a traditional back page only a serial matching `intake.serial_patterns` (or an e-stamp ID) counts;
  MP backs carry the vendor register no. (e.g. `1691735`) which the OCR API returns as certification_no.

## Open / next
1. **Per-state samples** (agreed plan): user puts one sample PDF per state in a local `samples/` folder (gitignored –
   customer data). Write a checker script that runs each through the real OCR API + intake checks WITHOUT Drive/DB
   and prints one line per file (state, pages, ID read, amount, OK / error code). Fix failing states (usually a
   per-state `intake.serial_patterns` entry, e.g. MH serials look like `99AB 794915`), turn each OCR reply into a test.
2. **M-3** (not built): on a transient OCR failure during the issue check, keep the LAN issued but UNVERIFIED and
   recheck next cycle; needs ADR-0007 + CLAUDE.md update. Today it goes to ERROR (safe).
3. Go-live items outside code: production OCR URL in `.env` (`ESTAMP_OCR_URL`), rotate the OCR API key, Sikkim legal
   sign-off (SK is now active).
4. MP papers already in Needs Review from the E107 bug: re-upload copies, or APPROVE in Review Actions with the serial.
5. Known: a running loop holds up to 12 DB connections – two loops at once can exhaust the DB pool.
