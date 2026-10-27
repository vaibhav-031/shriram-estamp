# 03: Pairing safety – first broken pair and all later pairs → Needs Review

Status: ready-for-agent

**What to build:** after OCR, each traditional pair must have exactly one serial, on the front page, and none on
the back. At the first pair that fails, that pair and every later pair of the same upload go to Needs Review with
"pairing broken at page N" (E107); earlier good pairs are stocked. Each digital page must carry exactly one Paper ID.

**Blocked by:** 02

- [ ] Pure pairing-check function; unit tests: all good, break in the middle, break at pair 1, serial on back, two serials on front
- [ ] Front and back pages OCR'd separately so their IDs are known
- [ ] Cycle test: missing back page in the middle → earlier pairs in Inventory, that pair and all later ones in Needs Review with the page number
- [ ] Cycle test: digital page with zero or two IDs → that page in Needs Review, others stocked
- [ ] Re-run after a crash mid-file stocks nothing twice

## Comments
- 2026-10-27: done. Front and back OCR'd separately; first_broken_pair() (pure) – front must have exactly 1 ID, back none (incl. an ID the API returns directly); that pair and all later → Needs Review E107 with the page. Digital page with 0/2 IDs → review alone. Crash mid-upload → rerun stocks nothing twice (test). Safety net: a STATE-SERIAL page inside a digital file → review (E105), never stocked as 1 page.
