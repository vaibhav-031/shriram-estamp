# 10: Major-issue emails through the Gmail API (ADR-0005)

Status: ready-for-agent

**What to build:** major issues (catalogue major flag) are emailed to `alerts.email_to` through the Gmail API from
the signed-in account: the same code + object at most once per hour, several in one cycle combined into one digest.
Slack and SMTP alerting are removed; the heartbeat URL ping stays.

**Blocked by:** Phase D complete

**Safety:** adding the `gmail.send` scope needs a one-time re-login by Vaibhav; no real email is sent from tests.

- [ ] Subject `[Shriram eStamp] <code> <title>`; body = message, what the system did, what to do, control-sheet link
- [ ] Tests with a mocked Gmail client: major vs not routing, hourly dedup, one digest per cycle, no key/customer name/full OCR text in any body
- [ ] First E301 per state + amount per day emailed; later ones only in the daily summary
- [ ] `check-setup` reports Gmail scope present and heartbeat URL set
- [ ] README login steps mention the re-login

## Comments
