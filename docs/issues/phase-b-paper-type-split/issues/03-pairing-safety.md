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
