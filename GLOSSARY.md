# Shriram eStamp

Vendor stamp papers are received, checked and stocked, then issued one per loan account to Shriram Finance as PDFs.
The words below are the ones code, tickets, sheets and ops messages should use.

## Papers

**Paper**:
One physical or digital stamp paper that can be issued to exactly one LAN.
_Avoid_: stamp, certificate (for the whole thing), unit, piece

**Digital e-stamp**:
A paper generated as a PDF by an e-stamp system; always one page.
_Avoid_: e-stamp certificate, SHCIL paper, digital paper

**Traditional paper**:
A pre-printed non-judicial stamp paper scanned by the vendor; always two pages, front and back.
_Avoid_: physical paper, scanned paper, judicial paper

**Paper type**:
Whether a paper is a digital e-stamp or a traditional paper; decided per uploaded file.

**Front page / back page**:
The two scanned sides of a traditional paper; the serial is printed on the front only.

**Paper ID**:
The single identifier that makes a paper unique: the certificate number of a digital e-stamp (fallback: its Unique Doc. Reference), or `<STATE>-<SERIAL>` for a traditional paper.
_Avoid_: cert (for traditional papers), serial (for digital e-stamps)

**Stamp duty**:
The amount in rupees printed on a paper; with the state it decides which LANs the paper can serve.
_Avoid_: denomination, value, face value

**Allowed amount**:
A stamp duty the state configuration accepts for that state.

**Active state**:
A state whose papers may be stocked and whose LANs may be issued. Kerala and Sikkim are not active.

## Stock

**Vendor**:
A supplier of papers (Paper Ease, Agreement Express, Shabda, …); identified by a vendor code.

**Vendor folder**:
The Upload Inbox subfolder for one vendor; a paper's vendor is the vendor folder it was uploaded into.

**Upload**:
One file ops drop into a vendor folder; it may hold many papers.
_Avoid_: batch, document

**Pairing**:
Matching each front page of a traditional upload with the back page that follows it.

**Inventory**:
All papers that passed every check and are AVAILABLE to be issued.
_Avoid_: stock pool, store

**Needs Review**:
Papers (or whole uploads) that failed a check and wait for an ops decision.
_Avoid_: rejected (that is a separate outcome), quarantine (see below)

**Quarantined paper**:
A paper pulled out at issue because the issued PDF did not show its Paper ID; never issued again without a human.

**Stray file**:
A PDF found in Inventory that the ledger does not know about.

**Correction**:
An ops edit to the Paper ID or stamp duty the OCR API read, stored next to the API's own reading.
_Avoid_: override, manual fix

## Orders and issue

**LAN**:
A Shriram loan account number; each LAN receives at most one live paper. Two LANs are the same LAN when their
**LAN keys** (all whitespace removed, uppercased; leading zeros kept) are equal – ADR-0008.
_Avoid_: order, loan, customer

**Order file**:
A Shriram sheet of LANs (with state) dropped into Orders Incoming.
_Avoid_: LAN sheet, request file

**Conflict**:
A LAN received for two different states; nothing is issued until ops keep one state.

**Issue**:
Giving one Inventory paper to one LAN: the paper is renamed `<LAN>.pdf`, placed in Output and recorded in the ledger.
_Avoid_: allocate (in ops-facing text), assign, map

**Issued PDF**:
The `<LAN>.pdf` file Shriram receives.

**Issue check**:
The second OCR of the issued PDF; it must show the Paper ID the ledger gave that LAN.
_Avoid_: verify OCR, second OCR (in ops text)

**Re-map**:
After a failed issue check, giving the same LAN the next Inventory paper once, automatically.
_Avoid_: reassign, retry

**Manual issue**:
A paper ops issued by hand while the service was down, recorded afterwards in the Manual Issues tab.

**Shortage**:
LANs waiting because no Inventory paper of their state and stamp duty is AVAILABLE.

## Operations

**Cycle**:
One pass of the service over all steps; repeats every two minutes in `run` mode.

**Ledger**:
The Postgres record of every upload, paper, LAN, issue and event; the source of truth.
_Avoid_: database (in ops text), DB

**Control sheet**:
The Google Sheet ops watch and act in.
_Avoid_: dashboard (in ops text)

**Review Action / Order Action**:
A row ops type into the control sheet to decide on a Needs Review paper or a LAN.

**Error code**:
The `E###` code that names one kind of problem, with one ops message and one ops action.

**Daily Report**:
Papers issued to Shriram LANs per state and stamp duty for a date or range; shared with Shriram.

**Vendor Reconciliation**:
Per vendor, state and stamp duty: papers received, issued, in Inventory, in Needs Review and rejected, with the amount payable.
