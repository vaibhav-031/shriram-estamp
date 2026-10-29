# G-01 — Production-readiness audit (report only)

Scope: the core modules in `estamp/` against the HANDOFF §9 checklist. Findings are grouped by
severity, Critical first. Each gives file:line, a concrete reproduction, and a proposed fix
(described, not implemented). This is an audit — **no code was changed**.

Overall the invariant that matters most — never issue a LAN twice — is genuinely well defended:
`allocations_order_live_uq` (`db.py:116`), the `WHERE id=%s AND status='ALLOCATING'` guard in
`finish_allocation` (`db.py:350-355`), and `FOR UPDATE SKIP LOCKED` in `claim_orders`/`reserve_paper`
(`db.py:320-330`) together make double-issue hard to reach even under crash + concurrency. The
duplicate-cert stock path is also solid: the partial unique index `papers_cert_live_uq` (`db.py:59`)
is the real guard and every insert catches `UniqueViolation` rather than trusting the advisory
`is_duplicate_cert` read. The findings below are the gaps around that core.

---

## Critical

### C-1 — Cancel + re-order of the same LAN on the same day is silently dropped from the Output sheet
**File:** `estamp/outputs.py:43-50`
**Checklist:** money correctness (cancellations reflected), idempotency.
The output writer dedups by LAN ID against what is already in the day's sheet:
`present = {row[0] for row in read_range(...A2:A)}` then `if r["lan"] in seen: continue`. This is
correct for crash-retry idempotency, but it treats *any* prior appearance of the LAN as "already
written". Flow that breaks it:
1. LAN `X` is allocated → an `ISSUED` row for `X` is written to `OUTPUT/<date>/<STATE>/<label>`.
2. Ops CANCEL `X` → `actions._order` appends a `CANCELLED – do not use` row for `X` (same sheet).
3. Same day, `X` is re-ordered and re-allocated (allowed: `allocations_order_live_uq` excludes
   cancelled rows, so a fresh allocation is legal).
4. `run_outputs` sees `X` already present in column A and **skips the new ISSUED row** →
   `mark_written` still flags the allocation, so it is never retried.

Result: a validly re-issued certificate never reaches Shriram's sheet, and the only visible row for
`X` that day says "do not use". A legal/money-visibility bug.
**Reproduce (test shape):** fake Drive sheet pre-seeded with an `ISSUED` row for LAN `X`; one
unwritten allocation for `X` with a different cert; run `run_outputs`; assert the new cert row was
appended (currently it is skipped).
**Fix:** dedup on `(lan, cert_no)` or on allocation id, not LAN alone — the crash-retry case it is
guarding against re-fetches the *same* allocation id, so keying on the allocation/cert is both
idempotent and reissue-safe. Alternatively, write a per-allocation marker column and skip only on
exact allocation-id match.

---

## High

### H-1 — `ocr_text` (customer PII) is persisted in Postgres and mirrored into alerts/Log
**File:** `estamp/intake.py:388` (`ocr_text=u.text[:20000]`), surfaced via `dashboard.py:114` (Log tab)
and `db.event` (`db.py:227`).
**Checklist:** security — no customer PII in logs/DB.
The full OCR text of a stamp paper (names, addresses, consideration amounts — real customer PII) is
stored verbatim in `papers.ocr_text` and in `ocr_raw` JSONB, then rendered into the Log tab and can
appear in event/alert `details`. CLAUDE.md explicitly calls out "no customer PII in logs/DB". There
is no redaction and no retention/TTL on `events`/`papers`.
**Reproduce:** stock any traditional paper; `SELECT ocr_text FROM papers` shows the full page text;
the Log tab shows event details that may echo cert/text fragments.
**Fix:** store only the fields the checks need (cert, state, amount, the matched ID) rather than the
whole page; or encrypt/omit `ocr_text` and keep it out of the dashboard. Add a retention policy that
prunes `events` and `ocr_text`/`ocr_raw` after N days (the `ponytail:` note at `context.py:149` already
flags `alerts_sent` growth — same pruning job can cover these).
**Fixed:** (PO decision "keep fields, prune text") the checks and `recheck-review` run on the stored OCR API
`fields`; free page text is not needed (only legacy text-reader papers have no fields – once pruned, recheck-review
leaves them in review as before; Re-OCR / Approve still work). A cycle step (`cycle._prune` → `DB.prune_retention`,
idempotent, once a day, not in dry run) clears `papers.ocr_text` and everything in `ocr_raw` except `fields`
`retention.ocr_text_days` (30) after stocking, and deletes `events` older than `retention.events_days` (90 – only the Log tab reads
events, newest 500; the ledger tables, not events, are the record of issue). `Ctx.event` now redacts Postgres
row/parameter echoes (`Failing row contains …`, `CONTEXT: …`) before the log file, events, Log tab and alerts, and a
failed OCR call no longer echoes the raw response body. Tests: `tests/test_retention.py`. `alerts_sent` is not pruned.

### H-2 — Token-file / secrets permissions are never checked or enforced
**File:** `estamp/settings.py:128-137` (loads `.env`), service-account/token loading in
`google_clients.py` (credentials from `secrets/`).
**Checklist:** security — token-file permissions.
Nothing verifies that `secrets/` and `.env` are not world-readable, and nothing fails go-live if they
are. On a shared host a `0644` service-account key is a full-Drive compromise. `check-setup` validates
folder ids and channels but not file modes.
**Reproduce:** `chmod 644 secrets/*.json .env`; the service starts and runs normally.
**Fix:** at startup (and in `check-setup`) `os.stat` the secret files and refuse to run — or warn
loudly — if the mode is group/other readable (`st_mode & 0o077`).

### H-3 — `_read_tabs` silently reads blank/stale values for formula-driven or value-less Excel
**File:** `estamp/orders.py:48-58`
**Checklist:** data edge cases — formulas, empty/huge Excel.
`load_workbook(..., data_only=True)` returns the *cached* result of each formula. A sheet produced by
a tool that did not store cached values (LibreOffice headless, some exporters) yields `None` for every
formula cell → the LAN/state/amount columns read as blank. Those rows then fail as "LAN blank" or
"state could not be determined" and are counted as invalid — but a whole file of formula cells could
look like a header-only file and be rejected with a misleading reason. Also `read_only=True` streams,
which is good for huge files, but there is no row cap: a multi-million-row junk file is parsed in full
inside the 20%-of-cycle ingest budget and can starve allocation/intake.
**Reproduce:** upload an `.xlsx` whose LAN column is `=CONCAT(...)` saved without cached values →
every row "LAN blank". Separately, a 2M-row file → ingest consumes the whole cycle budget.
**Fix:** detect all-`None` formula columns and raise a clear `E201` ("file has formulas with no cached
values — re-save from Excel"); cap rows per tab (config) and reject/short-circuit oversized files.

### H-4 — A 429 on either API pauses the shared limiter while holding its lock, blocking every worker
**File:** `estamp/http_api.py:51-64` (`acquire` sleeps while holding `self.lock`) + `:99-101`
(`limiter.pause` on 429, up to `MAX_RETRY_AFTER_S` = 60 s).
**Checklist:** performance at ~10.8k papers/day with the 5 rps OCR limit; API failures (429).
`RateLimiter.acquire` deliberately holds `self.lock` across its `sleep` so callers are served in order.
Combined with `pause(up to 60s)` on a 429, a single 429 freezes **all** OCR worker threads for up to a
minute per occurrence. Under the 5 rps cap the daily budget is ample in theory, but a provider that
429s intermittently serializes the whole process into 60 s stalls, and the intake step is time-boxed
to 80% of the cycle (`cycle.py:58`) — repeated pauses can push an upload past its budget and leave it
for next cycle indefinitely. Throughput math: intake reads 1 call/page and allocation re-OCRs every
issued page in `verify_issued` (`allocate.py:39-41`), so steady-state is intake pages + verify pages
on one shared 5 rps limiter; at 10.8k papers that is tens of thousands of calls/day funneled through
one serialized gate.
**Reproduce:** fake OCR returning one 429 with `Retry-After: 60`; assert all worker threads block for
~60 s (not just the one that got the 429).
**Fix:** don't sleep under the lock — compute the wait inside the lock, release, then sleep (re-check on
wake). Keep `pause` as a shared deadline but let threads back off independently. Consider separate
limiter headroom so verify-OCR doesn't starve intake.

### H-5 — Per-paper re-OCR (E-06) is not idempotent across a crash and bills a second OCR call every retry
**File:** `estamp/intake.py:375-383` (`_stock_unit` calls `_reread` when `not a.ok`).
**Checklist:** idempotency at every write; API failures / cost.
`pages_done` (`db.py:268`) makes the whole-upload re-run skip pages already in `papers`. But within a
unit, if the process crashes *after* `_reread`'s OCR call and *before* `insert_paper`, the next cycle
re-runs the unit and issues the extra OCR call again. Not a correctness bug (no row is written twice),
but every crash-retry re-bills one OCR call per not-yet-stocked failing paper, and there is no cap on
how many times a chronically-failing upload re-OCRs before the give-up window (`intake.py:327`) moves
it to review.
**Reproduce:** inject a crash between `_reread` and `insert_paper` in a fake; re-run; assert OCR was
called twice for the same page.
**Fix:** persist the retry reading (or a "retried" marker) before the failing paper lands, so the
re-run adopts the stored reading instead of re-calling; or gate `_reread` on a per-upload attempt count.

---

## Medium

### M-1 — Folder/sheet creation races are only guarded within a single process
**File:** `estamp/google_clients.py:141-162` (`folder_path`, `_folder_lock` + `_folder_cache`),
`estamp/outputs.py:19-28` (`output_sheet_id`, `_sheet_lock`).
**Checklist:** concurrency — folder/sheet creation races.
The in-process `_folder_lock`/`_sheet_lock` and the `output_sheets` table (`db.py:384-389`,
`save_output_sheet` is `ON CONFLICT DO NOTHING`) prevent duplicate folders/sheets *within one run*.
The process advisory lock (`db.py:210`) means there is normally only one run, so this holds in
practice — but it is a correctness-by-single-process assumption, not enforced structurally. If the
advisory lock is ever lost mid-run (see M-2) two runs could each `create_sheet` for the same
`(date,state,label)` because the list-then-create in `folder_path`/`create_sheet` is not atomic in
Drive. The DB table dedups the *id we remember*, but Drive would hold two sheets and the second
`save_output_sheet` would be a no-op pointing at the first — the orphaned second sheet silently
collects nothing.
**Reproduce:** two clients sharing a fake Drive, same `(date,state,label)`, concurrent
`output_sheet_id` → two `create_sheet` calls.
**Fix:** keep the single-process guarantee as the contract (document it), and make `create_sheet`
reuse an existing child by name first (like `folder_path` does), so a racing create resolves to the
same sheet.

### M-2 — Advisory lock can be silently lost mid-run (its own connection, no liveness check)
**File:** `estamp/db.py:210-224` (`try_lock` opens a dedicated `_lock_conn`; the lock lives for that
session only).
**Checklist:** concurrency — process advisory lock; DB loss mid-run.
The advisory lock is held on a separate connection opened in `try_lock`. If that connection drops
(network blip, Postgres restart, idle-timeout killer) the lock is released by Postgres while the cycle
keeps running on pool connections — a second process could then acquire the lock and run concurrently,
defeating the single-writer model the whole concurrency story rests on. Nothing re-checks the lock
during the cycle.
**Reproduce:** close `_lock_conn` mid-cycle in a test; start a second `run_cycle`; it acquires the lock
and proceeds in parallel.
**Fix:** re-assert the lock (or check `pg_advisory_lock` ownership) at the start of each heavy step, or
run the whole cycle inside the lock connection's transaction scope, or add a keepalive. At minimum,
detect `_lock_conn` closure and abort.

### M-3 — `verify_issued` OCR failure (timeout/5xx) sends a correctly-issued LAN to ERROR
**File:** `estamp/allocate.py:147-172`
**Checklist:** API failures (OCR 5xx/timeout) during allocation; money correctness.
After the paper is backed up and moved to Output, `verify_issued` re-OCRs it. A `CertMismatch` is the
intended ERROR path, but any *other* exception from `ctx.ocr.read` (timeout, 5xx after retries,
unreachable) propagates to the `except Exception` at `:163` and the LAN+paper both go to ERROR — even
though the move already succeeded and the cert is very likely correct. A flaky OCR provider during the
allocation window converts good issuances into a pile of ERRORs a human must RETRY.
**Reproduce:** fake OCR that raises `RuntimeError("timeout")` only on the verify call; assert the LAN
lands in ERROR despite a correct move.
**Fix:** distinguish "verification inconclusive" (transient OCR failure) from "verification failed"
(mismatch). On transient failure, either retry verification next cycle (leave the allocation in a
`verify_pending` state) or skip verification with a loud WARN rather than failing the issuance. Today
`verify_ocr` is all-or-nothing per paper.

### M-4 — No OCR size/page guard: a password-protected, 0-page, or huge PDF fails deep instead of early
**File:** `estamp/ocr_split.py:66-79` (`_split_local`), `intake.py:158-159` (empty → RuntimeError),
`_ocr_zoop` base64-encodes the whole file (`ocr_split.py:171`).
**Checklist:** data edge cases — 0-page/password PDFs, rotated/huge scans.
0-page is handled (`_split_local` → empty → "PDF has no pages"). But a *password-protected* PDF makes
`PdfReader` raise inside `_split_local`/`merge_pdfs` with a pypdf error that surfaces only as the
generic intake failure ("stays in the inbox, retried next cycle") and loops until the give-up window —
no E-code telling ops "this file is encrypted". A very large scan is base64-encoded in full into the
JSON body with no size cap, risking an OCR-side 413 that is only seen after upload+encode cost.
Rotated scans are entirely at the OCR provider's mercy with no detection.
**Reproduce:** feed an encrypted PDF to intake → repeated generic failures, no actionable code.
**Fix:** catch pypdf's encryption/parse errors explicitly and raise a `WholeUploadReview` with a clear
code ("password-protected — remove the password and re-upload"); add a config max-bytes check before
the OCR call.

### M-5 — `move_rename` does a read-then-update that can wipe parents under concurrent moves
**File:** `estamp/google_clients.py:173-179`
**Checklist:** concurrency; network loss mid up/download.
`move_rename` reads the file's current parents, then `addParents`/`removeParents` based on that read.
It is called from intake workers, allocation workers, and ops actions. Two concurrent operations on
the same file id (e.g. ops approve + allocation reserve racing on an orphan) would each compute
`removeParents` from a now-stale parent list and could move the file somewhere unexpected — which then
relies on the self-heal path. Within one process the per-cert `_stock_name_lock` and per-order
reservation make same-file concurrency unlikely, so this is latent rather than active.
**Reproduce:** two threads `move_rename` the same file to different parents; assert final parent set is
indeterminate.
**Fix:** pass the expected current parent explicitly (callers usually know it) instead of re-reading,
or serialize moves per file id.

### M-6 — LAN normalization is upper+strip only: internal spaces and leading zeros are significant
**File:** `estamp/extract.py:415-417` (`lan_key = self.lan.strip().upper()`), used as the live-order
uniqueness key (`db.py:81`).
**Checklist:** data edge cases — LAN with spaces/lowercase/leading zeros.
Lowercase and surrounding whitespace are normalized, which is good. But an *internal* space
(`"LAN 123"` vs `"LAN123"`) or a leading zero (`"0012345"` vs `"12345"`) produce different `lan_key`s,
so the same real loan account arriving in two forms is treated as two LANs — duplicate detection and
the cross-state CONFLICT guard both miss it, and both could be issued papers.
**Reproduce:** ingest `"LN 001"` then `"LN001"`; both become live PENDING orders; both get allocated.
**Fix:** decide the canonical LAN form with Shriram and normalize into `lan_key` (strip all internal
whitespace; decide on leading-zero policy). Keep the raw `lan` for display. This is a domain decision —
record it in an ADR.

### M-7 — `active_run_age_seconds` can misreport a wedged run after a hard crash
**File:** `estamp/db.py:240-246`, consumed at `cycle.py:31-36`.
**Checklist:** observability; concurrency.
The wedged-run escalation reads `max(started_at) WHERE finished_at IS NULL`. A process that crashed
hard (power loss) leaves a `runs` row permanently unfinished. If the lock is taken by a healthy new run
while an old crashed `runs` row is still `finished_at IS NULL`, and later a third cycle can't get the
lock, `active_run_age_seconds` returns the age of the *crashed* row (huge), firing a false "wedged"
ERROR+page. The comment claims "a hard crash drops the lock, so … the newest unfinished run is it" —
but the newest unfinished run can be a stale crashed one, not the live holder.
**Reproduce:** insert a `runs` row with old `started_at`, `finished_at NULL`; start a healthy run;
from a third connection fail to get the lock and call the wedged path → false positive.
**Fix:** mark crashed runs finished during `recover_stuck`, or scope the age query to the run that
actually holds the lock (store the lock's backend pid / run id).

---

## Low

### L-1 — Missing folder id / key errors are per-step, not pre-flight
**File:** `estamp/settings.py:78-82` (`folder()` raises only when first accessed), `ocr_split.py:167-170`.
**Checklist:** config errors (missing folder id / key).
`folder()` and the OCR/split config checks raise a clear message, but only lazily when a step reaches
them. A missing `folders.output_root` is not noticed until allocation runs, mid-cycle, after intake
already did work. `check-setup` exists but these are not asserted up front in `run`.
**Fix:** validate all required folder ids and API keys once at startup (and in `check-setup`) so a
misconfig fails before any Drive/DB work begins.

### L-2 — settle window trusts Drive's `modifiedTime`; no guard for a file missing that field
**File:** `estamp/intake.py:87-88`, `orders.py:28-29`, `google_clients.py:241-243`.
**Checklist:** data edge cases; API robustness.
`f["modifiedTime"]` is assumed present and ISO-parseable. A Drive item missing that field (rare, but
possible for some shared-drive items) raises `KeyError`/`ValueError` inside the per-file loop; intake
catches it per file (`intake.py:100`) and retries forever, orders rejects the whole file.
**Fix:** default a missing `modifiedTime` to "now" (so it waits out the settle window) and log once.

### L-3 — Alert fan-out at 40 lines is unsorted, so a critical can be the one dropped
**File:** `estamp/cycle.py:102-105` (`ctx.alerts[:40]`), `db.py:227-229` (`details[:20000]`).
**Checklist:** observability — every failure produces an error code + a Log row.
Every failure path does produce an event row (observability is otherwise strong), but a burst of >40
alerts in one cycle shows only the first 40 in Slack/email with a "… N more" pointer to the Log tab;
alerts are appended in step order, so during an incident the most actionable alert can be the 41st.
**Fix:** sort alerts ERROR-first before the `[:40]` slice so criticals are never the ones dropped.

### L-4 — `normalize_ocr`/`identify` are regex-heavy with no catastrophic-input guard
**File:** `estamp/extract.py:64-71`, `179-204`.
**Checklist:** data edge cases — huge OCR text, performance.
The OCR text passes through several `re.sub`/`finditer` passes. `ocr_text` is capped at 20k on storage
but the *input* to `normalize_ocr` during a cycle is whatever the reader returned, unbounded. Low risk
with the in-house OCR (structured fields, little free text), but the legacy text-reader path is
unbounded.
**Fix:** cap the text length fed to `normalize_ocr`.

---

## Notes on what is already solid (not findings)

- **No-double-issue:** `allocations_order_live_uq` + the `status='ALLOCATING'` CAS in
  `finish_allocation` (`db.py:350-355`) + `FOR UPDATE SKIP LOCKED` is a correct, layered defense.
- **Cancel-after-issue ordering:** `actions._order` commits the ledger *before* the Drive move and
  makes a failed Output-sheet write a LOUD ERROR (`actions.py:181-209`) — the stated invariant
  ("never issued-but-marked-returned") holds.
- **Auth failures** are cleanly separated from transient failures (`ApiAuthError`, never retried:
  `http_api.py:94-95`, `intake.py:319-323`), so a bad key fails loud instead of retry-storming.
- **Crash recovery** of stuck ALLOCATING/RESERVED rows is one transaction (`db.py:364-375`) and the
  advisory lock release is in a `finally` that always runs (`cycle.py:83`).
- **Output idempotency** for the crash-retry case it was designed for is correct — the gap (C-1) is
  only the reissue case.
