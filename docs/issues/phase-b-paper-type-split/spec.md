# Phase B – Paper type detection, split and pairing safety

Status: ready-for-agent

Source: HANDOFF.md §4, decisions D2, D3; ADR-0003; grilling 2026-10-27 (G3: traditional = always front + back).

## Problem Statement

Ops drop merged vendor files that are either all digital e-stamps (1 page each) or all scanned traditional papers
(front + back). Today pages are grouped into papers by guessing from their content, which can silently attach a
page to the wrong paper: if one back page is missing from a traditional file, every later front is paired with the
wrong back. Ops can't see where it went wrong, and a wrongly paired paper could be stocked and issued.

## Solution

Decide the paper type per upload before splitting, split by type through the split API (1 or 2 pages), and check
pairing after OCR. Unclear or mixed files and odd-page traditional files go whole to Needs Review. At the first
broken pair, that pair and everything after it in the file go to Needs Review; earlier good pairs are stocked.

## User Stories

1. As ops, I only choose the vendor folder; I want the service to work out whether a file is digital or traditional, so that I can't pick the wrong type.
2. As ops, I want a digital file split one page per paper, so that each e-stamp is one paper.
3. As ops, I want a traditional file split into front + back pairs, so that each traditional paper keeps both sides.
4. As ops, I want a file mixing digital and scanned pages sent whole to Needs Review with a clear reason, so that I split it and upload again.
5. As ops, I want a traditional file with an odd page count sent whole to Needs Review, so that no paper is stocked with a missing side.
6. As ops, I want the first broken pair (no serial on the front, or a serial on the back, or two serials) and every later pair of that file sent to Needs Review with "pairing broken at page N", so that one missing page can't shift every later paper.
7. As ops, I want the good pairs before the break stocked normally, so that one bad page doesn't hold up the whole file.
8. As ops, I want each digital page to carry exactly one Paper ID, otherwise that page goes to Needs Review.
9. As ops, I want the README to tell me how to fix a broken pairing (re-upload affected papers as separate 2-page PDFs, REJECT the broken entries).
10. As the tech team, I want the text layer used only to decide the type, never to read values (ADR-0002).
11. As the tech team, I want re-running intake on a partly processed file to give the same result, so that crashes don't duplicate or lose papers.

## Implementation Decisions

- **Type detection (pure function on page facts):** each page is classed as "has real text layer" or "image only" using a character threshold from config. All text → digital; all image → traditional; anything else → unclear/mixed (E105). Detection runs before the split API call.
- **Split size:** digital → `splitSize=1`; traditional → `splitSize=2`. Odd page count on traditional → whole upload to Needs Review (E106) before any split call.
- **Pairing check (pure function):** input is the ordered list of pairs with the IDs OCR found on the front page and on the back page separately. A pair is good when the front has exactly one serial and the back has none. The first bad pair index N → pairs N..end are flagged E107 "pairing broken at page P"; pairs before N proceed to normal checks.
- **OCR granularity:** each page of a traditional pair is OCR'd separately so front/back IDs are known (throughput impact noted in Phase A ticket 10).
- **Whole-file Needs Review:** the original file moves to Needs Review with the reason; a ledger row records the upload outcome so a re-run doesn't redo it.
- **Removed:** content-based grouping, `physical_front_markers` / `estamp_markers` grouping logic (markers may stay if type detection needs them as a tie-breaker – decided during implementation, recorded in the ticket).
- **Error codes** are written into the reason text now (E105/E106/E107); Phase C moves the wording into the catalogue.

## Testing Decisions

- Pure tests for type detection and the pairing check (no Drive, no DB): all-digital, all-scanned, mixed, near-threshold pages; good pairs, break in the middle, break at pair 1, serial on back, two serials on front.
- Cycle tests through the fake split + fake OCR clients: digital file, traditional file, odd pages, missing back page in the middle (pairs after it → Needs Review, pairs before → Inventory), mixed file, unreadable file.
- Re-run test: crash after some pairs are stocked → next cycle stocks nothing twice.
- Prior art: `test_physical_papers_front_and_back`, `test_estamps_never_grouped` – keep the cases that still apply, rewritten for the new rules.

## Out of Scope

- Vendor folders and vendor codes (Phase D).
- Catalogue wording (Phase C).
- Front-page-only OCR optimisation (only if Phase A ticket 10 says it's needed, and only after approval).

## Further Notes

- Traditional = always front + back is confirmed (2026-10-27). If a vendor ever sends fronts only, every such file will land in Needs Review as E106/E107 – that is intended.
