# ADR-0019: Time tracking lives in one row, two flavours

* **Status**: accepted (v0.14)
* **Context**: People asked for both *timers* (start, do work, stop) and
  *manual log entries* (after-the-fact "I did 45 minutes on this on
  Tuesday"). The temptation is to model these as two tables.
* **Decision**: One `time_entries` row. `source` distinguishes
  `timer` from `manual`. Open timers have `ended_at IS NULL`; manual
  entries always have both bounds.

## Why one table

* Reporting joins are 50% cheaper. A "total tracked on this task" query
  doesn't need a `UNION`.
* Schema migrations stay symmetrical — new columns added to "time"
  cost one alter, not two.
* The "open timer" invariant ("only one running timer per owner") is
  trivially expressible: `WHERE ended_at IS NULL AND owner_id = ?`.

## The single-open-timer invariant

`start_timer` does **not** error if the owner already has an open
timer. Instead it stops the existing timer at the new timer's
`started_at`. This matches what users actually want — they're saying
"I switched tasks", not "let me first stop the old one then start the
new one in two API calls". The implicit stop is recorded as a separate
audit event (`time.timer_stopped_implicit`).

## What we deliberately don't do

* **No timesheets-as-a-first-class-thing**. Reporting endpoints
  aggregate raw entries on demand. If a team needs frozen weekly
  timesheets, that's a downstream module on top.
* **No billing rates**. Money is out of scope; LabFlow tracks effort,
  not invoices.
