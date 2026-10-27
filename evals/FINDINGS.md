# Stress-eval findings & RCA

What the live e2e stress-eval surfaced, with root cause and the exact change to make.
Regenerate by re-running the `stress-eval` skill. Classify every finding as **harness**
(the eval mis-modeled input), **pipeline code**, or **document/format** before acting.

Last run: 2026-10-28, `python evals/stress_eval.py --lans 10 --papers 8 --reset --live --max-cycles 3`
(all active states, live Drive/OCR/Postgres).

## Verified correct (no action)

5 of 6 active **Digital/SHCIL** states — AS, KA, ML, UK, UP — passed fully at volume:
8 papers stocked and **8 certificates issued** each, 2 pending (shortage), and globally
**no paper issued twice, no LAN got two papers, every issued file is `<LAN>.pdf`, the
second-OCR cert matched the ledger, no stuck orders.** 40 real issuances, all correct.
Inactive KL correctly quarantined; Traditional states correctly refused (see below).
The pipeline logic is sound — all failures below are document-format or harness, not logic bugs.

---

## Finding 1 — Manipur (MN) papers never stock → all LANs pending

**Symptom:** MN AVAILABLE 0 (expected 8), orders DONE 0, PENDING 10. Paper rejected to review:
`state of 'IN-LF68436568245552Y' is not in state_config.csv`.

**OCR probe (real API, one MN page):**
- `certification_no = IN-LF68436568245552Y`   ← the 2 letters after `IN-` are **LF**, not MN
- `unique_doc_reference = SUBIN-MNMN-SELF68436568245552Y`   ← this **does** encode MN
- `state_code = None`, `state_name = (none)`, `stamp_duty_amount = 100`

**Root cause (`estamp/extract.py:307-310`, `id_from_fields`):** when the cert starts with `IN-`,
the state is taken from the 2 letters captured by `cert_patterns` (`\bIN-([A-Z]{2})…`) — here
`LF`, which resolves to no state. The `state` fallback from the `state_code`/`state_name` OCR
fields (line 306) was empty because OCR returned neither. The UDR branch (`SUBIN-…`, line 311),
which would yield `MN`, is **never reached** because the `IN-` branch returns first. So no state →
rejected. Verdict: **document format** (Manipur's cert embeds non-state letters, likely from
"SELF") exposing a **code assumption** that `IN-XX ⇒ XX is the state`.

**Fix (pick one or combine):**
1. In `id_from_fields`, when the cert's embedded code doesn't resolve to a known state, fall back
   to the UDR-derived state and/or `state_code`/`state_name` instead of returning `state=""`.
   I.e. don't treat the cert prefix as authoritative for the state.
2. Fix the OCR config so `state_code` is populated for Manipur (then line 306 rescues it).
3. The UDR `SUBIN-MNMN-SELF…` has a dash; `intake.estamp_ref_patterns` in `config.yaml` doesn't
   match dashes — extend it if the UDR path is used.

**Also — template data bug:** the `LAN-Stamp Mapping` tab for Manipur lists cert `IN-MN14847277`,
but the actual PDF reads `IN-LF68436568245552Y`. The template's oracle data for MN is inconsistent
with its own pages. Fix the mapping row (or the PDF).

---

## Finding 2 — Kerala (KL) whole file → Needs Review (inactive, latent)

**Symptom:** E105 "no page has a readable certificate / serial"; nothing stocked. KL is inactive,
so harmless today — but it will block Kerala the moment it's activated.

**OCR probe + image:** the document clearly prints `Certificate No : KL090744956` and
`Unique Doc. Reference : KL2026092357066750UDR` (clean, high-res — not an image-quality problem).
OCR returned `certification_no = {}` and `unique_doc_reference = {}` (empty), but read
`state_code = KL`, `stamp_duty_amount = 200`.

**Root cause:** Kerala uses a **non-SHCIL format**. Two gaps stack:
- **OCR config** doesn't extract Kerala's cert/UDR fields (returned empty).
- Even if it did, `id_from_fields` only handles `IN-…` and `SUBIN-…`; `config.yaml`
  `intake.cert_patterns` needs an `IN-` prefix and `estamp_ref_patterns` a `SUBIN-` prefix.
  `KL090744956` / `KL…UDR` match neither.
Verdict: **document format** → gaps in OCR config **and** repo patterns. Not a logic bug.

**Fix (before activating KL):** extend the OCR config to read Kerala's cert fields, **and** add a
Kerala cert/ref pattern (and state-derivation) in `config.yaml` + `id_from_fields`.

---

## Finding 3 — Traditional states (BR, TS, SK) untestable with current templates

**Symptom:** E106 "Traditional file, odd page count — every paper needs front + back." The harness
now auto-skips these with a clear reason.

**Root cause:** `pages_per_paper` (`estamp/extract.py:365-370`) returns 1 for e-stamps and the
state's `pages_per_paper` (default **2** = front+back) for physical/traditional papers. Bihar is
detected `physical` (serial `BE 176816`), so the pipeline expects 2-page pairs. The template PDFs
contain **fronts only** — 10 pages = 10 distinct serials = 10 fronts, no backs. Verdict:
**document/template** (missing backs), not a bug.

**Fix:** provide **front+back** sample PDFs for BR/TS/SK to test them. Alternatively, if Shriram's
real traditional papers are genuinely single-page, set `pages_per_paper=1` for those states in
`state_config.csv` (domain decision — confirm with the team / an ADR).

---

## Harness contract learnings (baked into the eval)

- **Vendor folder required** (`intake.py` E101): papers must sit in an inbox subfolder named after
  a known vendor (e.g. `Paper Ease`), never the inbox root. The eval uploads into the vendor folder.
- **Output `<LAN>.pdf` persists** (no-delete, A-09): re-issuing the same LAN collides and the
  pipeline correctly refuses to overwrite an issued cert. The eval suffixes each LAN with a unique
  `-E<tag>` per run so cycles don't collide.
- **`--reset` is DB-only** — it TRUNCATEs the schema but can't delete Drive files; test files
  accumulate in Drive. Wipe the Drive tree manually before go-live.
