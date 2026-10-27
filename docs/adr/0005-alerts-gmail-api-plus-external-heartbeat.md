---
status: accepted
---

# Alerts: Gmail API from the signed-in account, plus an external heartbeat

Major-issue emails, digests and the daily summary go through the Gmail API using the existing Google OAuth sign-in
(one more scope, one re-login), so no SMTP password is stored. Slack and SMTP alerting are removed. The external
heartbeat URL (`ESTAMP_HEARTBEAT_URL`) stays, because an email sent by the service cannot report that the laptop
itself is off or asleep (decided 2026-10-27). Recipients and the daily summary hour live in config. The same code
for the same object is emailed at most once per hour, and several events in one cycle become one digest email.
