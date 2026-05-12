# ADR-0027: Webhook dead-letter queue, in-table

* **Status**: accepted (v0.17)
* **Context**: Outbound webhooks retry up to `_MAX_ATTEMPTS = 5`. Until
  v0.17 a final failure left the row sitting at `attempts=5,
  success=False` — invisible to operators and impossible to replay
  without poking the database.
* **Decision**: Add a single column `webhook_deliveries.dead_lettered_at`.
  The deliverer stamps it the moment a delivery hits the cap without
  succeeding. A small DLQ module (`webhook_dlq.py`) lists them, and
  exposes `replay` / `discard` endpoints with audit on every action.

## Why a column, not a separate table

* The data is identical to a normal `WebhookDelivery` — same
  subscription, same payload, same response history. Splitting would
  duplicate every column and force callers to UNION two tables to
  ask "did this event ever land?".
* `dead_lettered_at IS NOT NULL` is a cheap predicate that becomes a
  partial index later if needed.
* `replay()` clears the column and resets `attempts`; the regular
  delivery loop picks it up. No second loop, no second worker.

## Audit trail

`webhook_dlq.replay` and `webhook_dlq.discard` each emit an audit row
with the actor. The chain (ADR-0011) makes silent edits detectable —
useful for compliance audits where a rogue admin re-firing a
sensitive webhook needs to be traceable.

## What we sacrifice

* No automatic alerting. The DLQ is observable via
  `GET /api/webhook-dlq/stats` (and the TUI) but doesn't auto-page
  anyone. Bringing in PagerDuty/Opsgenie would be premature; users
  can wire their own alert via a scheduled report on the audit log.
