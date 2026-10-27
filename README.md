# Shriram eStamp – Python service

Ops upload vendor PDFs to one Drive folder. Shriram's state-wise LAN sheets go in another.
The service splits, OCRs, checks and files every paper automatically, matches papers to LANs,
and writes the PDFs + sheets Shriram receives. Postgres is the ledger; a Google Sheet is the dashboard.

```
Shriram eStamp (Python)/
├── 📥 Upload Inbox        ← ops: drop vendor PDFs here – single, merged, or whole folders (any name).
│                            Never create state folders: every paper is filed by the cert printed on it.
├── Upload Archive/DATE    ← originals after processing (never deleted)
├── ⚠️ Needs Review/DATE   ← papers that failed a check (reason in the dashboard)
├── Inventory/STATE/AMOUNT/<CERT>.pdf      ← created automatically
├── Orders Incoming        ← Arpit: drop Shriram's LAN sheets here
├── Orders Processed / Orders Rejected
├── Output/DATE/STATE/     ← <STATE-AMOUNT> sheet (LAN | Cert | PDF link) + "<STATE-AMOUNT> PDFs/<LAN>.pdf"
└── Backup/DATE/STATE/AMOUNT/<LAN>__<CERT>.pdf
```

## 1. One-time setup on your Mac (≈30 min)

**Python**
```bash
brew install python@3.12
cd shriram-estamp-python
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

**Google sign-in (OAuth, runs as you)**
1. console.cloud.google.com → create a project (e.g. "shriram-estamp").
2. APIs & Services → Library → enable **Google Drive API** and **Google Sheets API**.
3. OAuth consent screen → User type **Internal**.
4. Credentials → Create credentials → OAuth client ID → **Desktop app** → download the JSON
   → save it as `secrets/client_secret.json`.
5. `python -m estamp login` → browser opens → allow. Token is saved to `secrets/token.json`.

If the Cloud Console or the consent screen is blocked, your Workspace admin has to allow it.

**Postgres – pilot on your Mac (no password, no approvals):** install Postgres.app from postgresapp.com,
open it, click Initialize, then in `.env` set
`ESTAMP_DB_URL=postgresql://<your-mac-username>@localhost:5432/postgres` and run `python -m estamp init-db`.
The ledger then lives on your Mac only – fine for the pilot, not for go-live.

**Postgres (Zoop's existing server, for go-live)** – ask whoever manages the DB to run this once, so the
service can only ever touch its own schema:
```sql
CREATE USER estamp_user WITH PASSWORD '<strong password>';
GRANT CONNECT ON DATABASE <zoop_db> TO estamp_user;
CREATE SCHEMA shriram_estamp AUTHORIZATION estamp_user;
```
Strongly prefer the **staging** database for the pilot. Then:
```bash
cp .env.example .env      # fill ESTAMP_DB_URL (add ?sslmode=require if the server needs SSL)
python -m estamp init-db
python -m estamp check-setup
```

## 2. Pilot run

1. `config.yaml` ships with `dry_run: true`.
2. Put a few sample papers in **📥 Upload Inbox** (the five sample LAN sheets are already in Orders Incoming).
3. `python -m estamp run-once` → open the control sheet → **Dry Run Preview** shows where every paper
   would be filed and which paper every LAN would get. Nothing is moved, nothing is written to the ledger.
4. Happy? Set `dry_run: false`, `python -m estamp run-once` again, check Output / Backup / dashboard.
5. Continuous: `caffeinate -i python -m estamp run` (caffeinate keeps the Mac awake).

## 3. Daily operation

| Who | Does | Where |
|---|---|---|
| Ops | Upload vendor PDFs | 📥 Upload Inbox |
| Arpit | Drop Shriram's LAN sheets | Orders Incoming |
| Ops | Fix flagged papers: add a row `Paper ID · APPROVE/REJECT · (corrected Cert/State/Amount)` | Control sheet → **Review Actions** |
| Ops | Cancel / retry / resolve a LAN: `LAN · CANCEL/RETRY/KEEP_STATE · (State)` | Control sheet → **Order Actions** |
| Everyone | Watch stock, queues, errors | Control sheet → Summary, Stock, Needs Review, Exceptions, Log |

The service fills the Result column of the action rows; it never edits what ops typed.

**Checks on every paper:** a single certificate number; its state code is a known, active state;
the stamp duty printed on the paper is that state's amount (from `state_config.csv`); no duplicate
certificate; optional required text (e.g. "SHRIRAM"). Anything else → Needs Review.

**Second OCR at issue (traceability):** after a paper is renamed to `<LAN>.pdf` and placed in
Output/DATE/STATE, that exact file is OCR'd again. The certificate printed on it must be the one the ledger
assigned, otherwise the PDF is pulled into `Needs Review/DATE/Cert mismatch`, the LAN and paper go to ERROR,
and nothing reaches the Output sheet. The verified certificate and time are stored per LAN (ledger
`allocations.verified_cert / verified_at`) and shown in the Output sheet columns "Cert Verified On Issued PDF"
and "Verified At" – so every LAN can be traced back to the exact certificate on the PDF Shriram received.
Switch: `allocation.verify_ocr` in config.yaml.

**Checks on every LAN row:** LAN format; state from the column → tab name → file name (Shriram's
spellings like KARTNATAKA / UTTARPRADESH / DELHI NCR / KOLKATA are mapped); state active; amount
allowed for that state; duplicates in the file and in the ledger. The same LAN sent for two different
states is held as CONFLICT until ops picks one.

## 4. Commands

```
python -m estamp check-setup   # DB, sign-in, every folder, control sheet, state config, APIs, alerts
python -m estamp run-once      # one cycle (respects dry_run; --dry-run / --live override)
python -m estamp run           # loop every loop_seconds (refuses while dry_run is on)
python -m estamp status        # stock + queue summary in the terminal
python -m estamp reconcile     # files in Inventory the ledger doesn't know; LANs stuck in ERROR
```
Logs: `logs/estamp.log` (rotating), the **Log** tab, and the `events` table.

## 5. Config you will touch

* `state_config.csv` – one row per state from the daily projection: amount, article, daily qty,
  `buffer_days` (low-stock alert below daily_qty × buffer_days), `active`, extra spellings, cert codes.
  Kerala (challan) and Sikkim (legal risk) ship **inactive**.
* `config.yaml → intake.cert_patterns` – add a per-state pattern once you have sample papers.
* `config.yaml → intake.required_text` – e.g. `["SHRIRAM"]` once samples confirm the party name prints.
* `config.yaml → zoop_api` + `intake.split_provider / ocr_provider: zoop_api` – when the in-house APIs are ready.
* Alerts: `.env` → `ESTAMP_SLACK_WEBHOOK`, and **`ESTAMP_HEARTBEAT_URL`** (e.g. healthchecks.io) so you
  are told if the Mac sleeps or the service stops.

## 6. Tests

```bash
pip install pytest reportlab
ESTAMP_TEST_DB_URL=postgresql://localhost/test python -m pytest -q tests/
```
The end-to-end tests use their own `estamp_test` schema and an in-memory Drive.

## 7. Moving off the Mac

Run the same code on a server or in Docker with `google.auth: service_account`. Service accounts
have no My Drive storage, so the folders must live in a **Shared Drive** that the service account can
edit (and set `folders.ocr_temp` to a Shared Drive folder).
