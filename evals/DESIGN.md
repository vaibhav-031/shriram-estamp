# Stress-test eval loop — design

## Intent

A discovery tool, not a regression guard: feed the **real** pipeline a wide spread of
inputs built from real template assets, run it end-to-end against the live service
(real Drive, real OCR/split APIs, real Postgres schema — pre-production, no real
customers yet), and verify the output against a ground-truth oracle. Failures tell us
what to fix in the code.

## What we build from

`evals/templates/`:
- `pdf/*_x10.pdf` — 10 states × 10 real stamp papers (image-only, OCR-readable).
  Digital e-stamps and Traditional (physical) variants.
- `orders/*.xlsx` — real loan lists. Two tabs each:
  - `Sheet1` — the order rows ops would upload (`LOAN NO`, `STATE`, …; **no**
    stamp-duty column — amount is inferred from the state's single allowed denomination).
  - `LAN-Stamp Mapping` — **the oracle**: every LAN → certificate, stamp duty,
    source PDF + page.

## Probe findings (already run against the live OCR API)

- Unmodified template page → OCR returns `certification_no`, `state_code`,
  `stamp_duty_amount` correctly. Real certs read perfectly.
- Overlaying a fabricated cert onto the image **broke** the `certification_no` read
  (everything else still read). So **cert-overlay fabrication is out for v1** — too
  fragile, needs per-template calibration.
- Decision: **repeatability via reset** (option 1). Use real papers untouched; clear
  the eval's own DB rows between runs so run N+1 sees no duplicates.

## The two scenarios

**Scenario 1 — orders + papers (allocation correctness).** Upload an order sheet
(as a Google Sheet in `orders_incoming`) and the matching paper PDFs (to
`upload_inbox`). Run cycles until drained. Verify every LAN got a valid paper.

**Scenario 2 — direct inbox uploads (intake).** Drop multiple paper PDFs straight
into the Drive `upload_inbox` (no orders), including messy files. Run intake. Verify
each page landed in Inventory with the right cert/state/amount; messy → Needs Review.

## Scenario parameters (the "generate the assets" knobs)

Per run, per state group:
- which states to include
- `#LANs` and `#papers` (set them unequal to force shortage / surplus / exact)
- messy toggles: duplicate upload (same file twice), non-PDF file, nested folder of PDFs

Order sheets replicate the real format faithfully (no stamp-duty column) so the
inference path is exercised. The oracle is computed from the `LAN-Stamp Mapping` tab
filtered to the chosen subset + the parser's known rules.

## Oracle & invariants (asserted, independent of the code under test)

Scenario 1:
- each LAN issuable iff its state is active and amount resolvable → DONE; else shortfall → PENDING
- every allocation's paper is a valid paper of that LAN's state+amount (cert ∈ mapping's set)
- no paper issued twice; no LAN gets two papers
- issued file sits at `…/{state}/{state}-{amount} PDFs/{LAN}.pdf`; re-OCR'd cert == ledger cert
- nothing left stuck mid-state after drain

Scenario 2:
- every good page → one AVAILABLE paper with cert/state/amount matching the mapping
- duplicate upload → one DUPLICATE, no extra paper
- non-PDF → Needs Review / Not PDF; nested-folder PDFs → stocked, folder archived
- no good paper lost

## Runner

Standalone script `evals/stress_eval.py` (real `estamp.google_clients.Google`, real
`DB`, real settings from `config.yaml`):
- CLI: `--states`, `--lans`, `--papers`, `--scenario orders|inbox|both`, `--seed`,
  `--iterations`, `--reset`.
- Loop: `reset → build & upload assets → run_cycle until drained → verify → report`.
- A failing run prints the seed + which invariant failed + the offending LAN/cert/path.
- `--reset` scoped-deletes only the eval's rows (keyed by the template LANs/certs).

## Safety

- Pre-production live service; fabricated data is acceptable (confirmed by owner).
- No Drive deletes (A-09 rule) — re-uploaded copies accumulate; wipe Drive + schema
  manually before go-live.
- Reset touches **only** rows whose LAN/cert belong to the template set.

## Deferred (ponytail)

- Cert-overlay fabrication for >100 unique papers / non-destructive runs — revisit with
  per-template calibration if volume beyond the templates is needed.
- Wrong-amount / inactive-state / no-cert *bad-paper* injection — already covered by the
  existing fake-based unit tests; add here only if real-infra behavior needs confirming.
