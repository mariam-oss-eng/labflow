# ADR-0015: Recurring tasks live in the same job queue

* **Status**: accepted (v0.12)
* **Context**: v0.12 needs a way to express "create the post-mortem
  task every Friday at 09:00 UTC" without bolting on a new scheduler
  process. We already ship an in-process job queue (v0.3) backed by
  the `jobs` table.
* **Decision**: model recurring tasks as a single declarative table
  (`recurring_tasks`) and materialise them via a sweep that the
  existing worker calls every minute. No new process, no new
  dependency.

## Why a separate table — not a "repeat" column on `tasks`?

Materialising into the `tasks` table on the schedule means:

* Each fired instance has its own ID, audit history, comments, and
  evidence — the same affordances every other task has.
* Closing the materialised task does not affect the schedule.
* A schedule can be paused (`active=false`) without losing history.
* Forecasting (v0.10) and burndown numbers naturally include or
  exclude recurring instances based on whether they're actually due
  in the window.

A boolean column on `tasks` would have collapsed all of these.

## Cadence model

Three cadences land in v0.12, all expressible as `(cadence, interval,
day_of_week, day_of_month)`:

* `daily`   — every `interval` days.
* `weekly`  — every `interval` weeks on `day_of_week` (0 = Monday).
* `monthly` — every `interval` months on `day_of_month`. We **clamp**
  to the last day of short months (so "31st" becomes 28/29 in
  February). Skipping makes us forget; clamping reflects the operator
  intent ("end of month").

Cron strings would have been more expressive but introduce a parser
(and an entire timezone discussion). We can add `cron` as a fourth
cadence later without breaking existing rows.

## Idempotency

The sweep advances `next_run_at` *before* committing the new task, so
re-running the sweep within the same minute is a no-op. The audit
event records both the new task ID and the new `next_run_at`, so
operators can see the full schedule history in `/api/audit`.

## Consequences

* Recurring tasks share the worker, retries, and observability of
  every other job.
* They go through the same `/api/tasks/bulk` and Kanban surface as
  any other task.
* When we add cron later, it slots in as a fourth `cadence` value;
  the materialiser is the only thing that changes.
