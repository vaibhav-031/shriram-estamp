# 02: Split by type – 1 page digital, 2 pages traditional; odd pages → Needs Review

Status: ready-for-agent

**What to build:** digital uploads split one page per paper, traditional uploads split into front + back pairs, via
the split API. A traditional upload with an odd page count goes whole to Needs Review (E106) before any split call.

**Blocked by:** 01

- [ ] Split API called with `splitSize` 1 or 2 by type
- [ ] Cycle tests: digital file → N papers; traditional 2N-page file → N two-page papers in Inventory
- [ ] Cycle test: traditional odd-page file → whole file in Needs Review, reason names E106, split API not called

## Comments
- 2026-10-27: done. splitSize 1/2 by type through ctx.splitter (local or split API); odd traditional page count → whole upload REVIEW (E106) before the splitter is called. Parts that are not exactly 1/2 pages → upload error, nothing stocked, retried.
