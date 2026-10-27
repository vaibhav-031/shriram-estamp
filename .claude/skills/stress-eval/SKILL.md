---
name: stress-eval
description: Run the live end-to-end stress-test eval (evals/stress_eval.py). Use when the user wants to stress-test the pipeline, run the eval loop, verify allocation/intake against the oracle, or check whether expectations match after a change.
---

# Stress-test eval

A discovery tool, not a regression guard: it builds assets from the real templates in
`evals/templates/`, uploads them to **live** Drive, runs real cycles (real OCR/split APIs,
real Postgres), and verifies the output against the `LAN-Stamp Mapping` oracle. Failures tell
you what to fix. Design + rationale: `evals/DESIGN.md`.

## Safety — read before running

- **This issues real certificates against the live `shriram_estamp` schema.** Only run on a
  pre-production environment (no real customer LANs). `config.yaml` is locked to `dry_run: true`;
  the eval needs `--live` to override it. Confirm the owner's sign-off before a live run.
- **Drive has no delete** (A-09): every run leaves `EVAL_*` files in Drive and rows in the
  schema. Wipe the Drive tree + schema manually before go-live.
- `--reset` **TRUNCATEs the whole schema** first. Fine pre-prod; never on a schema with real data.
- Slack alerts fire during runs (low stock, etc.) — expected, noisy.

## Pre-flight

```bash
source .venv/bin/activate
python -m estamp check-setup --live   # DB, Drive folders, OCR/split, alerts all ✓
```

## Run it

```bash
source .venv/bin/activate
# tight single cycle (cheap, ~40s) – prove the loop end-to-end on one clean state:
python evals/stress_eval.py --scenario orders --states KA --lans 2 --papers 2 --reset --live --max-cycles 1

# volume across all active Digital states (shortage → pending), both scenarios:
python evals/stress_eval.py --lans 10 --papers 8 --reset --live --max-cycles 3
```

`mise stress` is a shortcut, but the common flags (`--states`, `--reset`, `--live`) make the CLI clearer.

### Flags

- `--scenario orders|inbox|both` — allocation flow, direct inbox-upload intake, or both (default `both`).
- `--states KA,AS,...` — subset (default: all templates). Active Digital states work; Traditional
  (BR/TS/SK) auto-skip (templates are front-only); inactive (KL) routes to review.
- `--lans N` / `--papers N` — per state; set them unequal to force shortage/surplus.
- `--reset` — TRUNCATE the schema first (repeatability). `--tag X` — fixed LAN suffix (default random,
  so re-runs don't collide on persisted Output files).
- `--dup-upload` — also upload an exact-copy duplicate per group (exercises E104 dedup).

## Reading the report

Each line is an invariant checked against the oracle: `✓` = expectation met, `✗` = mismatch to
investigate. The money-critical invariants: no paper issued twice, no LAN gets two papers, every
issued file is `<LAN>.pdf`, and the second-OCR cert matches the ledger.

When a `✗` appears, first decide **harness vs pipeline**: is the eval mis-modeling the input, or is
the pipeline wrong? A failing state often means a non-SHCIL certificate format (the OCR config
and/or `intake.cert_patterns` don't recognize it) — classify it **code vs document** by OCR-probing
one real page (`estamp.ocr_split._ocr_zoop`) and checking what `certification_no`/`state_code` come
back. For a genuine pipeline bug, switch to the `diagnosing-bugs` skill before changing code.

## Known gaps (as of the last run)

- **Manipur (MN)**: cert reads `IN-LF…` (the 2 letters aren't the state); pipeline derives state
  from those letters → rejected. Fix = derive state from `state_code`/UDR, not the cert prefix.
- **Kerala (KL)**: non-SHCIL `KL…` format; OCR + `cert_patterns` don't read it (inactive, latent).
- **Traditional (BR/TS/SK)**: need front+back sample PDFs; current templates are fronts only.
