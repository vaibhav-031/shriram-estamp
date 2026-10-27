# Phase H – Hand-off to dev (last)

Status: ready-for-agent

Source: HANDOFF.md §10, §11, decisions D9, D10.

## Problem Statement

The pilot runs on one laptop, under one person's Google account, against dev API endpoints with a key that was
shared in chat. The dev team taking it over needs the data in the procurement Shared Drive, a service account,
production endpoints with a rotated key, and documentation that lets both ops and developers run it without the
original author.

## Solution

Move all folders to the procurement Shared Drive, switch to a service account, swap to production OCR/split URLs
with the new rotated key, give a storage estimate and backup retention proposal, and write the README as an ops
guide plus a dev guide (architecture, commands, error catalogue, runbook from HANDOFF §11).

## User Stories

1. As the dev team, I want every folder in the procurement Shared Drive, so that the data doesn't depend on one person's account.
2. As the dev team, I want the service running under a service account.
3. As security, I want the dev key retired and a rotated production key in `.env` only.
4. As the business, I want a storage estimate (GB/day, GB/month) and a backup retention proposal.
5. As ops, I want a plain-language ops guide with the runbook from HANDOFF §11.
6. As a developer, I want a dev guide: architecture, commands, error catalogue, runbook.

## Implementation Decisions

- Folder move done by Vaibhav or with his go-ahead per step; folder IDs in config updated and proven by `check-setup`.
- Service-account mode already exists in the code; the move tests it for real.

## Testing Decisions

- `check-setup` passes in service-account mode against the Shared Drive.
- One supervised `run-once` after the switch, approved by Vaibhav.

## Out of Scope

- Any new features.

## Further Notes

- Open (HANDOFF §14): production OCR/split URLs, new key, split API auth.
