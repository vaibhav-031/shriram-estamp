# Shriram eStamp – Handoff (Claude Code + dev team)

## Current status (2026-10-27) – read this first

**Works today (pilot code; `config.yaml` is locked to `dry_run: true` – a live run needs Vaibhav's OK):** Upload Inbox → pypdf page split → read
each page from its PDF text layer, else **Google Drive OCR** → pages grouped into papers by content → checks →
`Inventory/<STATE>/<AMOUNT>/` or Needs Review. Order files → PENDING LANs → issue with `FOR UPDATE SKIP LOCKED` →
issue check (second OCR) → output sheets, control sheet, Slack/SMTP/heartbeat alerts. The in-house OCR/split API
code is an unused stub written against a guessed contract.

**Committed:** this plan, `GLOSSARY.md`, `docs/adr/0001–0005`, specs + tickets in `docs/issues/`, CLAUDE.md
amended to ADR-0001. Code: A-01 volume test (~1,000 LANs, zero double issue), A-02 README fixes, A-04 OCR/split
clients injected via the run context (`tests/fake_apis.py`), **A-06 split API adapter** (contract captured from live
calls with synthetic PDFs in `docs/api-samples/split/`; no auth; returns a zip of `-<from>-<to>.pdf` parts; does
NOT reject odd page counts). `config.yaml` still uses `split_provider: local` – switching to `zoop_api` sends every
upload to live.zoopsign.com (Vaibhav to decide). Suite: 45 passed + 1 expected-fail (A-08 target).

**Not built (as of the original 2026-10-27 write-up):** everything else below.

**Audit 2026-10-28 (code-verified).** Phases A (in-house APIs) and B (paper type / split / pairing) have since
landed; `config.yaml` now runs `ocr_provider` + `split_provider: zoop_api`. The shortlist below is annotated with
code-verified status markers: ✅ done · 🟡 partial · ❌ not built. Done so far: A-03/A-05/A-06/A-07 (OCR+split
adapters, rate-limit+retry), all of Phase B, and the service-account + Shared-Drive *capability* (not the move).

**Test data reset (2026-10-27, by Vaibhav's decision – all of it was test data):** every table in `shriram_estamp`
truncated, ID sequences reset (all 8 tables are data; setup lives in `state_config.csv` / `config.yaml`, unchanged);
37 files in the Drive working folders moved to Trash (recoverable 30 days), folders and control sheet kept. No DB
backup was taken. From now on, test only in dry run: `python -m estamp run-once --config config.dryrun.yaml --dry-run`
(local, untracked file: scratch schema `estamp_dryrun`, "Dry run inbox" folder, control-sheet copy, alerts off).

**Known issues from the first dry run (22 sample papers, 18 read):** 4 would stock (2 HP e-stamps, 2 MP traditional
papers front + back); 14 → Needs Review:
- **Unrecognised Paper ID formats – HR, JH, MH** (6 papers): "no certificate / serial number found"; patterns needed.
- **Amounts missing in `state_config.csv`** (8 papers): JK ₹100 (allows 350), OD ₹500 (allows 100), PB ₹40,000
  (allows 350), TN ₹100 (allows 200) – confirm with Shriram which amounts are real before adding them.
- **Drive OCR took 83 min on one TN page** and the cycle hit its time budget (whole run 92 min; 4 papers never
  read). Supports doing A-05 (OCR API) first, and every OCR call needs a **per-page timeout** (A-07).

**Shortlist for the dev team, in priority order** (tickets in `docs/issues/`; deferred tickets are `needs-triage`;
✅/🟡/❌ = code-verified status on 2026-10-28):
1. ❌ E-01 never append a LAN twice to an output sheet (real gap in today's live flow) – only the `output_written`
   flag guards it (`outputs.py:31-46`); no by-LAN check before `append_rows`, so a crash between append and
   `mark_written` can double-append.
2. ✅ A-03 capture redacted OCR API samples – `docs/api-samples/ocr/` (digital, traditional, not-pdf).
3. ✅ A-05 OCR API adapter at intake + issue check, raw response stored per paper, **per-page timeout** –
   `ocr_split.py:182` `_ocr_zoop`, `allocate.py:35` reuses `ctx.ocr`, `db.py:106` `ocr_raw`, `timeout_s`/`max_total_s`.
4. ❌ E-06 + E-08 (merged) corrections stored next to the API reading; issue check accepts either; auto second
   API attempt – none built: no audit fields, no RE-OCR action (`actions.py:62` APPROVE/REJECT only), no auto
   second attempt, `verify_issued` accepts only the ledger cert.
5. 🟡 A-07 ✅ rate limit + retry (`http_api.py`) → A-08 🟡 failure policy (file stays in inbox ✅ but xfail still
   strict + no "after M cycles" escalation) → A-09 ❌ remove Drive OCR (`ocr_split.py:158` fallback still present)
   → A-10 ❌ throughput measurement.
6. ✅ B-01…B-03 (+B-04 merged) paper type, 1/2-page split (`splitSize` 1/2; reject odd pages before calling it),
   pairing safety (E105/E106/E107) – `intake.py:139-186`, `extract.py:365/376`; content grouping removed.
7. 🟡 E-02 self-heal stuck issues (`db.py:315` only moves to ERROR, no Drive-location heal) → 🟡 E-09 crash-resume
   tests (2 of the required steps covered).
8. 🟡 E-10 + E-11 (merged) Gmail major-issue emails + CRASH/HEARTBEAT – Slack/SMTP/heartbeat exist
   (`context.py`); **no Gmail, no major-vs-minor routing, no CRASH/HEARTBEAT codes**. Keep the heartbeat URL.
9. ❌ C (one merged ticket) coded `✗ [E###]` lines – no `estamp/errors.py`, no `explain` command; only
   E105/E106/E107 emitted in code.
10. ❌ D-02 live Daily Report tab → D-03 `report --date` – no report subcommand, no Daily Report tab.
11. ❌ G-01 audit of intake + issue paths (no `docs/AUDIT.md`) → H-02 production URLs + rotated key (still dev
    endpoint) → H-04 README ops guide. (Service-account + Shared-Drive *capability* ✅ in `google_clients.py`.)
- ❌ F-03 `reset-test-data` command: still waiting on Vaibhav (the 2026-10-27 reset was done by hand, see above).
- Deferred: ❌ auto re-map (D7 – still allowed by ADR-0001, not built), ❌ vendors + reconciliation,
  ❌ stray-file adoption, ❌ Manual Issues tab, RECHECK ✅ / RE-OCR ❌, ❌ Drive declutter, ❌ Shared Drive move,
  ❌ storage estimate.

⚠️ **The OCR API key was shared in chat – rotate it before go-live.** Keys live only in `.env`; never commit them.

---

Owner: Vaibhav (APM, ZoopSign). Status: working pilot on a Mac; this document defines the work to make it
production-ready. Put this file in the repo root as `HANDOFF.md`. Rules in `CLAUDE.md` always apply.

---

## 0. How to work through this (Claude Code)

> Read `HANDOFF.md`, `CLAUDE.md`, `README.md`, then `estamp/` and `tests/`. Do not change code yet.
> 1. Summarise the system in 10 lines and list anything in this document that conflicts with the code.
> 2. Use `grill-with-docs` on any point that is ambiguous; ask me, don't guess.
> 3. Use `to-spec` → `to-tickets` to turn each phase below into tickets in `docs/issues/`.
> 4. Implement ONE phase at a time with `tdd`; `diagnosing-bugs` before fixing any failure; built-in
>    `code-review` before every commit. Run the suite 5× in a row. Stop for my approval after each phase.
> 5. Never touch real Drive folders or the real database without asking. Never print, log or commit secrets.

Phase order: **A** APIs → **B** paper type + split → **C** error messages → **D** vendors + money + reports →
**E** crash safety + alerts (section 12) → **F** declutter + reset → **G** production audit → **H** storage move (last, at hand-off).

---

## 1. What the system does today (context)

Python 3.15 service, Postgres (Postgres.app, port 3000, schema `shriram_estamp`), Google Drive + Sheets via
OAuth on the ops laptop. Runs `python -m estamp run` (a cycle every 2 min) or `run-once`.

| Step | Today |
|---|---|
| Upload | Ops drop vendor PDFs (merged/single/folders) into `📥 Upload Inbox` |
| Intake | split per page (pypdf) → read text (PDF text layer, else Google Drive OCR) → group pages into papers by content → checks → `Inventory/<STATE>/<AMOUNT>/<ID>.pdf` or `⚠️ Needs Review` |
| IDs | digital e-stamp: `IN-UP…` cert, fallback Unique Doc. Reference `SUBIN-UPUP…`; traditional paper: `<STATE>-<SERIAL>` e.g. `RJ-S254201` |
| Orders | Shriram Excel (`LOAN NO`, `STATE`; no stamp-duty column → amount from `state_config.csv`) in `Orders Incoming` → PENDING LANs |
| Allocate | LAN gets oldest AVAILABLE paper of same state + amount (`FOR UPDATE SKIP LOCKED`) → backup → rename+move to `Output/<date>/<STATE>/<STATE-AMOUNT> PDFs/<LAN>.pdf` → **second OCR on the issued file must show the same ID** → row in `<STATE-AMOUNT>` sheet |
| Control sheet | Summary, Stock, Needs Review, Exceptions, Log; ops actions in `Review Actions` (APPROVE/REJECT) and `Order Actions` (CANCEL/RETRY/KEEP_STATE) |
| Commands | `login, init-db, check-setup, run-once, run, status, reconcile, recheck-review` |

Proven: real Shriram Excel parsed; 97/100 real traditional RJ papers read correctly; 10 real UP digital
e-stamps stocked and issued live with second OCR passing; 10,530-LAN volume test (fake Drive) with zero double
allocation.

Non-negotiables (keep): never delete user files (move/rename/copy only) · reserve in DB before any Drive change ·
nothing ever issued twice · every step safe to re-run · ops-typed cells never overwritten · no customer PII stored.

---

## 2. Decisions taken (do not re-open without asking)

| # | Decision |
|---|---|
| D1 | **OCR = in-house API only.** Remove Google Drive OCR and the PDF-text-layer reading from the read path. On API failure: log, show a clear error, retry; never silently fall back. |
| D2 | **Split = in-house split API.** Digital e-stamp → 1 page per paper; traditional paper → always 2 pages (front + back). |
| D3 | Paper type is **auto-detected per file** (see Phase B). Ops only choose the vendor folder. |
| D4 | Rajasthan, Kerala, Sikkim and all other states: identification/amount come from the OCR API output; **all 27 states active**. Allowed amount per state still comes from `state_config.csv` (projection) – verify RJ with the first real Shriram RJ paper. |
| D5 | Ops manager report counts papers **issued to Shriram LANs**. No fixed day-close time. |
| D6 | Vendors: **Paper Ease, Agreement Express, Shabda** (more may be added). Vendor cost is entered by ops in the report (editable rate field + formulas), not computed from a price list. |
| D7 | Second-OCR mismatch at issue → **automatic re-map once** (bad paper quarantined, LAN gets the next paper); second mismatch on the same LAN → ERROR for a human. *(Assumption – confirm with Vaibhav before building.)* |
| D8 | Restart after reboot/crash: ops re-run the command (no auto-start needed now). |
| D9 | Storage: move everything to the **procurement Shared Drive at the very end**, when handing to dev (Phase H). |
| D10 | Test now with the dev OCR endpoint + the provided key; production URL/new key swapped at hand-off. |
| D11 | **The OCR API reads first; ops can correct.** Failed check → automatic second API attempt → Needs Review showing exactly what the API returned. Ops actions: `APPROVE` (optionally with edited ID/amount – same validations as intake: ID format for the state, allowed amount, no duplicate), `RE-OCR` (send to the API again), `RECHECK` (re-validate the saved API result after a config fix, no API call), `REJECT`. Every edit stores API value, ops value, who, when; reports flag "manually corrected". Second OCR at issue accepts the ops-corrected ID **or** the API's original reading for that paper; anything else is a mismatch. |

---

## 3. Phase A – In-house APIs

Secrets go **only** in `.env`: `ZOOP_OCR_API_KEY`, `ZOOP_OCR_ORG_ACC`, (split API auth if any). Never in
config, code, tests, logs or commits. The key was shared in chat → **must be rotated before go-live**.

### A1. OCR API
```
POST https://dev-ai.zoop.one/parsely-top-layer/v1/ocr/do
Headers: x-zoop-key: <ZOOP_OCR_API_KEY>
         x-zoop-org-acc: <ZOOP_OCR_ORG_ACC>
         Content-Type: application/json
Body:    {"mode":"sync","ocr_config_id":"ocrc_2f0e4e33ae22475f976e4787d62e08e2",
          "documents":[{"data":"<base64 of the page/paper PDF>"}]}
```
- **First task:** call it with real samples (one digital UP e-stamp page, one traditional RJ front page) and save the
  raw responses to `docs/api-samples/` (strip nothing sensitive? → redact names). Document the response shape.
- Map from the response: certificate no. / serial, Unique Doc. Reference, state, stamp duty amount, first party,
  raw text. Prefer structured fields if the API returns them; otherwise run the existing parsers on its text.
- Replace `ocr_split.read_paper` providers with this adapter only. Delete Drive OCR code paths and the PDF
  text-layer read path (keep `pdf_text` only if Phase B needs it for type detection – it is not used for reading).
- Measure and record: average seconds per call, payload size limits, behaviour on bad input.

### A2. Split API
```
POST https://live.zoopsign.com/api/pdf-utils/split   (multipart/form-data)
file=@<pdf>  ranges[0][startPage]=1  ranges[0][endPage]=<N>  splitSize=<1|2>
```
- First task: call with a 4-page sample, save response + headers to `docs/api-samples/`, document how parts come
  back (zip? JSON with base64/URLs? page numbers?). Confirm whether auth is needed.
- Adapter returns `[(page_from, page_to, bytes)]`.

### A3. Rate limit and failure handling (both APIs)
- Process-wide token bucket: **max 5 requests/second** to the OCR API (configurable), shared by all threads;
  respect 429/Retry-After; exponential backoff on 5xx/timeouts; split API gets its own limiter.
- Failure policy (D1): a page whose OCR keeps failing after N retries → the file stays in the Inbox and is retried
  next cycle; after M cycles (configurable, e.g. 30 min) → Needs Review with reason + alert. Auth failure (401/403)
  → stop intake for the cycle, loud error E113, no retry storm.
- Second OCR at issue uses the same adapter + limiter.
- Volume check: ~10.8k papers/day ≈ 25–35k OCR calls/day incl. second OCR. Measure real throughput at 5 rps and
  report whether a full day finishes in < 4 hours; if not, propose (don't implement) OCR of the front page only for
  traditional papers.

Tests: mock both APIs from the saved real responses; rate-limit test (no 1-second window > 5 calls with 50 parallel
requests); 401/429/500/timeout/garbage-JSON paths.

---

## 4. Phase B – Paper type detection + split + pairing safety

- Per uploaded file, detect type **before** splitting:
  - **digital** if the PDF has a real text layer (e-stamp generated PDF) → `splitSize=1`;
  - **traditional** if pages are scanned images (no/negligible text layer) → `splitSize=2`;
  - unclear or mixed → whole file to Needs Review, error E105.
  (Text layer is used **only** to decide the type, never for reading values – D1.)
- Traditional file with **odd page count** → reject to Needs Review, E106 (no partial stocking).
- After split + OCR, each traditional pair must have **exactly one serial, on the front page**, and none on the
  back. First pair that fails → that pair **and all later pairs of the same file** go to Needs Review, E107
  "pairing broken at page N" (prevents a missing back page shifting every later pair). Earlier good pairs stock
  normally.
- Digital: each page must carry exactly one ID.
- Ops fix for E107: upload the affected paper(s) again as separate 2-page PDFs into the vendor folder, and REJECT
  the broken entries in Review Actions. Document this in README (ops section).
- Remove the current content-based page grouping (replaced by split API + safeguards); keep its tests that still
  apply.

Tests: digital file, traditional file, odd pages, missing back page in the middle (pairs after it → review),
mixed file, unreadable file.

---

## 5. Phase C – Clear error messages for ops

Every problem the ops person can hit must print **one clear line** in the terminal (and the Log tab), in this form:

```
✗ [E111] OCR failed for 'Shabda_27Oct.pdf' page 14 (server error 503).
   What I did: will retry automatically next cycle.
   What you do: nothing now. If this keeps repeating for 30 minutes, tell the tech team.
```
Plus `✓` lines for successes and a one-screen summary at the end of each cycle
(received / stocked / in review / issued / waiting / errors). No stack traces on screen (they go to
`logs/estamp.log`). Implement as a central catalogue `estamp/errors.py` (code → message template → ops action) so
wording lives in one place. Catalogue:

| Code | When | What the system does | What ops do |
|---|---|---|---|
| E101 | File not inside a vendor folder | Leaves it, Needs Review "vendor unknown" | Move it into the right vendor folder |
| E102 | Not a PDF | Moves to Needs Review/Not PDF | Re-export as PDF and upload |
| E103 | PDF corrupt / password-protected | Needs Review | Get a clean file from vendor |
| E104 | Exact duplicate upload | Not stocked again | Nothing |
| E105 | Paper type unclear / mixed file | Whole file to Needs Review | Split digital and scanned into separate files, upload again |
| E106 | Traditional file with odd page count | Whole file to Needs Review | Find the missing page, upload again |
| E107 | Pairing broken at page N | That pair + later pairs to Needs Review | Upload those papers as separate 2-page PDFs; REJECT broken entries |
| E110 | Split API failed | Retries; then Needs Review | Nothing; tell tech if repeating |
| E111 | OCR API failed / timeout | Retries; then Needs Review + alert | Nothing; tell tech if repeating 30 min |
| E112 | OCR rate-limited | Slows down automatically (info) | Nothing |
| E113 | OCR/split API key rejected | Stops intake, alert | Tell tech team immediately |
| E120 | No certificate / serial found (after auto second API attempt) | Needs Review, shows API output | APPROVE with the correct ID from the paper, or RE-OCR, or REJECT |
| E121 | More than one ID on a paper | Needs Review | Check file; re-upload correctly |
| E122 | State unknown | Needs Review | Tell tech (state spelling) |
| E124 | Stamp duty unreadable (after auto second API attempt) | Needs Review, shows API output | APPROVE with the correct amount, or RE-OCR, or REJECT |
| E125 | Stamp duty not allowed for state | Needs Review, shows API output | If config was wrong: tech fixes, ops RECHECK; if paper is wrong: REJECT |
| E126 | Duplicate certificate / serial | Needs Review (never stocked twice) | REJECT; raise with vendor |
| E201 | Order file unreadable / wrong type | Moves to Orders Rejected | Send as .xlsx / Google Sheet |
| E202 | No LOAN NO column | Orders Rejected | Fix the Excel, drop again |
| E203 | LAN blank / invalid in a row | Row skipped | Fix row, re-send |
| E204 | State unknown in a row | Row skipped | Fix spelling / tell tech |
| E205 | LAN repeated / already received | Skipped | Nothing |
| E206 | Same LAN for two states | Both held (CONFLICT) | Order Actions: KEEP_STATE |
| E207 | LAN already issued earlier | Skipped | Nothing (or CANCEL if wrong) |
| E301 | No paper for state + amount | LAN waits, shortage alert | Upload papers for that state |
| E302 | Issued PDF shows different ID | Paper quarantined, LAN re-mapped (D7) | Check quarantined paper |
| E303 | Mismatch again on same LAN | LAN ERROR | Order Actions: RETRY after checking |
| E304 | Output file name already exists | LAN ERROR | Tell tech |
| E305 | Drive move/copy failed | Retries; then LAN ERROR | Tell tech if repeating |
| E306 | Recovered an interrupted issue | Fixed automatically (info) | Nothing |
| E401 | Output sheet write failed | Retries next cycle | Nothing |
| E402 | Reconciliation totals don't balance | Red row + alert | Tell tech |
| E501 | Google sign-in expired | Stops, alert | Run `python -m estamp login` |
| E502 | Database not reachable | Stops | Open Postgres.app, run again |
| E503 | No internet | Waits, resumes | Check Wi-Fi |
| E504 | Another run already active | Skips this run | Nothing |
| E505 | Drive storage low | Alert | Tell tech |
| E506 | Config error (folder/ID missing) | Stops with details | Tell tech |

Tests: each code is produced by at least one test; messages contain file/page/LAN context.

---

## 6. Phase D – Vendors, money and reports

### D-1 Vendors
- `vendors.csv`: `vendor_code, vendor_name, active` – seed: `PAPEREASE, Paper Ease`, `AGREXP, Agreement Express`,
  `SHABDA, Shabda`.
- Service creates one subfolder per active vendor in the Upload Inbox. Vendor of a paper = the vendor folder it
  was uploaded in (nested folders inherit). Root-level files → E101.
- Store `vendor_code` on every paper; show it in Stock, Needs Review, Issued detail.

### D-2 Ops manager report (shared with Shriram) – **issued to Shriram LANs**
Format (one row per state + stamp duty; a state with two amounts gets two rows; total at the end):

| State | Stamp Duty (₹) | Quantity | Amount (₹) |
|---|---|---|---|
| HR | 100 | 280 | 28,000 |
| … | | | |
| **Total** | | **Σ qty** | **Σ amount** |

Three ways to get it (all from the ledger, never typed numbers):
1. **Live tab** `Daily Report` in the control sheet – today's issued figures, refreshed every cycle (2 min).
2. **Command** `python -m estamp report --date 2026-10-27` → creates `Reconciliation/<date>/Daily_Report_<date>`
   as a **Google Sheet + .xlsx**, prints the **link**. Re-running for the same date replaces it (logged). No fixed
   day-close time (D5) – ops run it when they want the frozen copy to share.
3. Range option: `--from … --to …` for weekly/monthly totals.
Amount = quantity × stamp duty; use Sheet formulas for Amount and Total so edits recalc (xlsx skill).

### D-3 Vendor reconciliation (internal)
Same command also writes a `Vendor Reconciliation` tab/file:
- per vendor × state × stamp duty: papers **received**, **issued**, **in stock**, **in review**, **rejected/duplicate**;
- **editable column `Rate per paper (₹)`** (ops type it, D6) → formula columns `Payable (received × rate)`,
  `Disputed (rejected × rate)`, `Net payable`;
- check row: received = issued + in stock + in review + rejected (+ errors); mismatch → E402.
- Issued detail tab: LAN, state, amount, ID, vendor, issued at, output link.

Tests: report totals with mixed outcomes; two amounts in one state; re-run same date; balance check.

---

## 7. Phase E – Crash safety gaps

- **E-1 Self-healing stuck allocations** (replace manual RETRY after crashes): at cycle start, for LANs stuck in
  ALLOCATING / papers RESERVED > N min, check the file's real location in Drive: still in Inventory → release
  (paper AVAILABLE, LAN PENDING); already in Output as `<LAN>.pdf` → finish (backup if missing, second OCR,
  allocation row, DONE); elsewhere/missing → ERROR + alert. Log each decision (E306).
- **E-2 Stray files**: list `Inventory/**` each N cycles; any PDF not in the ledger → OCR + checks → register
  AVAILABLE (vendor "unknown") or Needs Review; log "stray file adopted" in the Log tab. Never delete.
- **E-3 Output sheet duplicates**: never append the same LAN twice (check by LAN before append).
- **E-4 Manual Issues tab** (system-down fallback): `LAN | ID | Issued by | Result | Processed At`. When ops issued
  a paper by hand, the service validates (LAN pending, paper available, same state + amount) and records it exactly
  like an automatic issue; never double-issues.
- **E-5 Auto re-map** (D7, after confirmation).
- **E-7 Review actions per D11**: keep `APPROVE` with optional edited ID/amount (full intake validations, audit fields
  `api_id, api_amount, ops_id, ops_amount, edited_by, edited_at`), add `RE-OCR` and `RECHECK`, keep `REJECT`; update
  the `Review Actions` tab header and README. Store every raw API response per paper (`ocr_raw` JSON) so Needs Review
  shows exactly what the API said. Automatic second API attempt before any paper lands in Needs Review. Second OCR at
  issue: accept ops ID or the stored API reading for that paper.
- **E-6 Resume guarantees** to prove by tests: crash at every step of intake and allocation (before/after backup,
  after move, after verify, during sheet write) → next run ends correct, nothing duplicated or lost.

---

## 8. Phase F – Declutter + test-data reset

Target layout (folders referenced by ID in config, so moves/renames are safe – verify with `check-setup`):
```
Shriram eStamp/
  1. 📥 Upload Inbox/        Paper Ease/  Agreement Express/  Shabda/
  2. 📥 Orders Incoming
  3. 📤 Output               date / state / sheet + LAN PDFs
  4. ⚠️ Needs Review
  5. 💰 Reconciliation       daily report + vendor reconciliation
  📊 Control sheet
  ⚙️ System (don't touch)/   Inventory, Backup, Upload Archive, Orders Processed, Orders Rejected
```
- Remove ONLY after Vaibhav confirms: `Old samples (wrong amounts)`, `TEST_Shriram_Order_Summary_26Oct`, old Apps
  Script folder `Shriram eStamp (Test)` + its control sheet, control-sheet tabs `Sheet1` and `Dry Run Preview`.
- `python -m estamp reset-test-data` (asks to type YES): clears test ledger rows, moves test files to a dated
  "Test archive" folder. Used once before go-live.

---

## 9. Phase G – Production-readiness audit (report first)

Write `docs/AUDIT.md` (Critical/High/Medium/Low, file:line, reproduction – ideally a failing test – and fix), then
fix Critical/High after approval. Cover: idempotency at every line · concurrency (process lock, threads, folder and
sheet creation races) · API failures (OCR/split/Google: 401/403/429/5xx, timeouts, malformed JSON) · token expiry ·
network loss mid upload/download · DB loss mid transaction · data edge cases (huge/empty Excel, extra tabs,
formulas, merged cells, LAN spaces/lowercase/leading zeros, 0-page/password PDFs, rotated scans, multi-page
e-stamps) · money correctness (reports always balance; cancellations reflected) · security (secrets, no PII in
logs/DB, token file permissions) · observability (every failure → error code + Log row) · performance at 10.8k/day
with the 5 rps limit · config errors.

---

## 10. Phase H – Hand-off to dev (last)

- Move all folders to the **procurement Shared Drive** (D9); switch to a service account; storage estimate
  (GB/day, GB/month) + backup retention proposal.
- Swap to production OCR/split URLs and the **new rotated key** (D10).
- README: ops guide (plain language) + dev guide (architecture, commands, error catalogue, runbook).

---

## 11. Ops runbook (manual cases) – also goes into README

| Situation | Ops action |
|---|---|
| API misread a paper (serial/amount) | Review Actions: `Paper ID · APPROVE · corrected ID · corrected amount` (validated, audited) – or `RE-OCR` to let the API try again |
| Bad scan / broken pairing | Rescan or extract that single paper (2 pages traditional, 1 digital), upload it to the vendor folder, then `REJECT` the old entry |
| Rule was wrong, not the paper (e.g. allowed amount) | Tech fixes config → Review Actions: `Paper ID · RECHECK` |
| Wrong/duplicate paper | Review Actions: `REJECT` (counts as disputed for that vendor) |
| Broken pairing (E107) | Upload affected papers again as separate 2-page PDFs in the vendor folder; REJECT broken entries |
| Same LAN, two states | Order Actions: `LAN · KEEP_STATE · <state>` |
| LAN must be cancelled | Order Actions: `CANCEL` (issued paper returns to stock; output sheet gets a CANCELLED row) |
| LAN in ERROR | Order Actions: `RETRY` |
| System down, must issue by hand | Take paper from Inventory, rename to LAN, place in today's Output folder, add sheet row, **record in Manual Issues** |
| Laptop restarted / crashed | Run `python -m estamp run` again – it continues where it stopped |
| Need the report for Shriram | `python -m estamp report --date YYYY-MM-DD` → open the printed link |

---

## 12. Visibility and alerts (who sees what)

### Ops team – terminal (every issue, as it happens)
- One clear line per event with the error code, what happened, what the system did, what ops must do (Phase C
  catalogue). End-of-cycle summary: received / stocked / in review / issued / waiting / errors.
- `python -m estamp explain E111` prints the meaning and the ops action for any code (same catalogue).
- `python -m estamp status` – stock per state, queues, last errors.

### Ops team – control sheet
| Tab | Shows |
|---|---|
| Summary | today's counts, last successful run |
| Stock | per state + amount: available, in review, pending LANs, days of cover |
| Needs Review | every flagged paper: reason + error code, **what the API read**, link to the PDF |
| Review Actions / Order Actions / Manual Issues | where ops act (service fills only Result) |
| **Corrections** (new) | audit of every manual edit: paper, API value → ops value (ID/amount), edited by, at, LAN it was later issued to |
| Exceptions | LANs in ERROR / CONFLICT |
| Daily Report | live issued-to-Shriram report (Phase D) |
| Log | every event with code |

### Email alerts – major issues only
Recipients (config `alerts.email_to`): **atul.kumar@zoop.one, vaibhav.sharma@zoop.one**.
Send via the Gmail API with the existing Google OAuth (add the `gmail.send` scope → one-time re-login), sender =
the signed-in account. No SMTP passwords.

Major = email immediately (deduplicated: the same code + same object at most once per hour; multiple events in one
cycle → one digest email):

| Code | Why it is major |
|---|---|
| E113 | OCR/split API key rejected – intake stopped |
| E111 / E110 | API failing for 30+ minutes |
| E126 | Duplicate certificate/serial – possible vendor fraud or double scan |
| E302 / E303 | Issued PDF did not match – wrong paper caught at issue |
| E304 / E305 | Issue step failed (output exists / Drive move failed repeatedly) |
| E402 | Reconciliation totals don't balance |
| E501 / E502 | Google sign-in expired / database down – everything stopped |
| E505 / E506 | Drive storage low / config error |
| E301 | First shortage of the day per state + amount (then included in the daily summary only) |
| CRASH | A cycle crashed (with log excerpt, no secrets/PII) |
| HEARTBEAT | No successful cycle for 30 minutes while `run` should be active (best effort while the laptop is on) |

Not emailed (ops handle in terminal/sheet): E101–E107, E112, E120–E125, E201–E207, E306, E401.

Plus a **daily summary email** at a configurable hour (default 20:00 IST): issued per state, stock flags, items in
Needs Review, manual corrections count, errors of the day.

Email content: subject `[Shriram eStamp] <code> <short title>`; body = plain-language message, what the system did,
what to do, link to the control sheet. Never include API keys, customer names or full OCR text.

Tests: alert routing (major vs not), dedup window, digest, no secrets/PII in body, Gmail API mocked.

---

## 13. Definition of done (every phase)

All tests pass 5× in a row · new behaviour covered by tests (incl. failure paths) · volume test (~1,000 LANs, decided 2026-10-27)
passes with zero double allocation · README updated · no new manual step for ops unless approved · committed with
a clear message after built-in `code-review`.

## 14. Still open (ask before building)

- D7 auto re-map – confirm.
- Rajasthan allowed amount (195 vs face value) – verify with the first real Shriram RJ paper via the OCR API.
- Kerala is challan-based in the projection – confirm the OCR API path produces a usable paper ID for it.
- Production OCR/split URLs, new key, split API auth – at Phase H.
