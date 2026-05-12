# ADR-0026: Scheduled reports — pull-driven, separate from event webhooks

* **Status**: accepted (v0.16)
* **Context**: Operators want a daily Slack ping with "every open bug
  assigned to platform". We already have outbound webhooks but they're
  *event-driven* — they fire on state changes, not on a clock.
* **Decision**: Introduce `ScheduledReport(query, cadence, webhook_url)`
  with its own queue and its own delivery history table. Sweeper
  `scheduled_reports.run_due()` picks reports whose `next_run_at` has
  passed, runs the LFQL query, POSTs the matching IDs, records a
  `ScheduledReportRun`, and advances `next_run_at`.

## Why not reuse `WebhookSubscription`

* Webhooks fan out on every emitted event, with retries shared across
  the whole subscription. Reports are *one* call per cadence, and a
  failure shouldn't block unrelated events from going out.
* Report history is its own thing — operators want "did the daily
  bugs report succeed yesterday?" without paging through tens of
  thousands of unrelated webhook deliveries.
* Cadence is part of the data model (`hourly|daily|weekly`), not part
  of an external scheduler. Self-contained makes the admin story one
  command (`POST /api/reports/_run-due`).

## Why HMAC-SHA256, optional secret

Same construction as v0.10 webhooks (`X-LabFlow-Signature-256`) so
receivers reuse one verifier. The secret is **optional**: many
internal Slack webhooks already carry their authentication in the URL.

## What we deferred

* Cron expressions. The three cadences cover the asked-for cases and
  keep the ops surface tiny. A cron grammar would multiply the test
  matrix without obvious wins.
* Per-report rate limits. Daily/weekly cadences make this a
  non-issue; the `hourly` cadence + HTTP timeout (`10s`) bounds
  worst-case load.
