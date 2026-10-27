# 10: Measure real throughput at 5 rps and report

Status: ready-for-human

**What to build:** a short report (in `docs/api-samples/`) of real OCR throughput at the 5 rps limit, and whether a
~10.8k-paper day (25–35k calls including the issue check) finishes in under 4 hours. If not, a written proposal
(not an implementation) for OCR of the front page only on traditional papers.

**Blocked by:** 07

**Safety:** live calls against the dev API; needs Vaibhav's go-ahead and a set of non-production sample PDFs.

- [ ] Average, p95 seconds per call and achieved calls/second recorded
- [ ] Projected hours per day for 25k and 35k calls
- [ ] Recommendation stated plainly (fits / doesn't fit, and the proposal if not)

## Comments
