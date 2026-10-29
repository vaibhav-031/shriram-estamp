---
status: accepted
---

# One canonical LAN key: all whitespace removed + uppercase; leading zeros stay significant

Every place a LAN is matched now uses one key, `canonical_lan()` in `estamp/extract.py`: **remove all whitespace**
(leading, trailing and internal: spaces, tabs, non-breaking and other Unicode spaces) **and uppercase a–z**. Nothing
else is changed – in particular **leading zeros are kept**, so `0012345` and `12345` stay two different LANs. The
LAN as received is still stored (`orders.lan`) and shown everywhere (Output sheet, `<LAN>.pdf`, Log), with only its
whitespace runs collapsed to one plain space so no tab / NBSP / run of spaces reaches a file name; only the match
uses the key (`orders.lan_key`). Decided by the product owner on 2026-10-29 (AUDIT M-6).

Uppercasing is ASCII-only on purpose: Python's `upper()` turns `ﬁ` into `FI` and `ß` into `SS`, which would let
pasted junk – in an order sheet or typed by ops into Order Actions – equal a real LAN.

Evidence: Shriram's sample order sheets (`evals/templates/orders/*.xlsx`, 10 states, 100 LANs) all have the same
shape – 7 capital letters + 10 digits, e.g. `TGHYDTW2610260001` – with no spaces. A LAN starts with letters, so a
"leading zero" can't occur in a real LAN, and stripping zeros would only ever merge LANs that might really differ.
Whitespace never belongs to a LAN, so a space typed or pasted by a human (`TGHYDTW 2610260001`) is the same loan.

Where the key is used: order ingest (duplicate-in-file, duplicate-in-ledger, cross-state CONFLICT), the live-order
unique index `orders_lan_live_uq`, ops' Order Actions (CANCEL / RETRY / KEEP_STATE) and Manual Issues, and the
Output-sheet re-append guard (LAN + certificate). `orders.lan_regex` is checked on the canonical key (so
`TGHYDTW 2610260001` passes). The regex itself was **not** tightened to the sample shape – not approved yet.

## Consequences

- Existing ledger rows: `init-db` re-keys any order whose `lan_key` isn't canonical (idempotent; touches only
  `lan_key`, never `lan`), and the CLI refuses to run until it has (`orders.lan_key not canonical` in the schema
  check). If re-keying would put two **open** (non-CANCELLED) orders that had different keys under one key, nothing
  is re-keyed or merged: `init-db` stops and names the rows; a human decides with Shriram which order is real and
  cancels the other by hand in the ledger (Order Actions can't, because the service refuses to run until then).
  Under the old `lan_regex` no stored LAN could contain whitespace, so on today's ledgers the re-key is expected to
  be a no-op.
- Deploy: stop the service, pull, `init-db`, start. A process started on the old code keeps writing old-style keys
  until it is restarted.
- The `<LAN>.pdf` name and the Output sheet keep the LAN as Shriram sent it, single spaces included.
- If Shriram ever confirms LANs with real formatting characters (`-`, `/`) that differ between systems, that is a
  new decision – this ADR only removes whitespace.
