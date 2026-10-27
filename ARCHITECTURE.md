# Architecture – a developer's map

New here? Read this, then `estamp/cycle.py` (the flow) and `GLOSSARY.md` (the words).
`README.md` is for operators (setup + daily use); this is for people changing the code.

## The one thing to understand: a cycle

The service does nothing but run **one cycle** over and over. Each step is safe to re-run,
so a crash or restart never corrupts state. Order (from `estamp/cycle.py`):

```
1. recover    stuck LANs/papers after a crash → ERROR (never silently retried)
2. actions    ops approvals / cancellations from the control sheet
3. ingest     new LAN sheets → PENDING orders
4. allocate   PENDING LANs ← AVAILABLE papers (uses stock on hand first)
5. intake     Upload Inbox → split → OCR → Inventory / Needs Review (time-boxed)
6. allocate   again, so papers stocked this cycle are issued this cycle
7. outputs    Shriram-facing sheets for everything issued
8. alerts     low stock, cycle summary, heartbeat
9. dashboard  rewrite the control-sheet tabs
```

A Postgres advisory lock means only one cycle runs at a time. Postgres is the source of
truth; Google Sheets are just a window onto it.

## Where the code lives

| Module | What it owns |
|---|---|
| `cycle.py` | The 9 steps above, lock, time budget, alert/heartbeat wrap-up |
| `extract.py` | **Pure** logic: state resolution, OCR parsing, paper checks, sheet parsing. No Google, no DB — keep it that way so it stays unit-testable |
| `db.py` | Postgres ledger (own schema only); claim/reserve use `FOR UPDATE SKIP LOCKED` |
| `google_clients.py` | Drive + Sheets wrapper (move/rename/copy only — **never delete**) |
| `intake.py` / `ocr_split.py` | Upload Inbox → split pages → OCR → group into papers → stock or review |
| `orders.py` / `allocate.py` | LAN sheets → PENDING orders → match a paper to each LAN |
| `outputs.py` / `dashboard.py` / `report.py` | Everything written back to Sheets |
| `actions.py` | Ops corrections (fills Result columns only, never edits ops input) |
| `context.py` / `settings.py` / `constants.py` / `errors.py` | Run context, config, folder names, the error catalogue |
| `http_api.py` | Rate-limit + retry for the in-house OCR/split APIs |

Tunables live in `config.yaml` and `state_config.csv`, never hard-coded. Error wording ops
see lives in one place (`errors.py`). Decisions that aren't obvious from the code are ADRs
(`docs/adr/`).

## Running it while you develop

```bash
make setup        # one-time: create the venv
make dev-safe     # repeat a dry-run cycle against a scratch schema (nothing real is touched)
make test5        # the 5×-in-a-row suite required before committing (CLAUDE.md)
make              # list every task
```

**Never** run `run-once` / `run` without `--dry-run` against real config — `config.yaml` is
LIVE and moves real Drive files. See the safety rules in `CLAUDE.md`.
