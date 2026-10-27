# 03: `report` command – frozen Daily Report as Google Sheet + .xlsx

Status: ready-for-agent

**What to build:** `python -m estamp report --date YYYY-MM-DD` (and `--from/--to`) creates
`Reconciliation/<date>/Daily_Report_<date>` as a Google Sheet and an .xlsx with formula Amount/Total, and prints the
link. Re-running the same date replaces it (old pair moved aside, never deleted), logged.

**Blocked by:** 02

- [ ] New `reconciliation` folder in config, checked by `check-setup`
- [ ] Sheet and xlsx have the same rows; Amount = quantity × stamp duty and Total as formulas
- [ ] Range option sums over the dates
- [ ] Test: re-run same date replaces and logs; nothing deleted

## Comments
