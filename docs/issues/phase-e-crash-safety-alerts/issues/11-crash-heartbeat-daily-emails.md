# 11: CRASH, HEARTBEAT and daily summary emails

Status: ready-for-agent

**What to build:** a crashed cycle sends a CRASH email with a log excerpt scrubbed of secrets/PII; no successful
cycle for 30 minutes while `run` is active sends HEARTBEAT (best effort); a daily summary email at the configured
hour (default 20:00 IST) lists issued per state, stock flags, Needs Review count, corrections count and the day's
errors.

**Blocked by:** 10

- [ ] Tests: crash → one CRASH email with scrubbed excerpt; 30 min without success → one HEARTBEAT; daily summary sent once per day
- [ ] Excerpt scrubber test with a fake key and a fake customer name

## Comments
