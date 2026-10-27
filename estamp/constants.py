"""Canonical names of every Drive folder the system uses.

The actual folder IDs live in `config.yaml` under `folders:` (they are
environment-specific and point at real Drive). This module only names the
*keys* so callers write `s.folder(UPLOAD_INBOX)` instead of a bare string –
a typo is caught at import, not at runtime against the live Drive.

Each constant's value must match a key under `folders:` in config.yaml.
"""

# ── Intake side (vendor stamp papers coming in) ──────────────────────────
UPLOAD_INBOX = "upload_inbox"        # ops drop vendor PDFs here (merged or single)
UPLOAD_ARCHIVE = "upload_archive"    # originals kept after processing – NEVER deleted
NEEDS_REVIEW = "needs_review"        # papers that failed a check; a human looks here
INVENTORY_ROOT = "inventory_root"    # STATE/AMOUNT/<CERT>.pdf – available stock, auto-created

# ── Orders side (Shriram's LAN sheets) ───────────────────────────────────
ORDERS_INCOMING = "orders_incoming"    # Shriram's state LAN sheets land here
ORDERS_PROCESSED = "orders_processed"  # order sheets moved here once consumed
ORDERS_REJECTED = "orders_rejected"    # order sheets rejected (bad format etc.)

# ── Output side (issued certificates) ────────────────────────────────────
OUTPUT_ROOT = "output_root"    # DATE/STATE/<STATE-AMOUNT> delivery sheet + issued PDFs
BACKUP_ROOT = "backup_root"    # DATE/STATE/AMOUNT/<LAN>__<CERT>.pdf – copy of each issued paper

# ── Infrastructure ───────────────────────────────────────────────────────
OCR_TEMP = "ocr_temp"    # scratch folder for the temporary OCR Doc; only needed with a service account
