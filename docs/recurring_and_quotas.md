# Recurring tasks & API key quotas

Two operational features added in v0.12.

## Recurring tasks

Templates that materialise into real tasks on a schedule.

### Create a template

```bash
curl -X POST http://localhost:8000/api/recurring-tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "slug": "weekly-postmortem",
    "cadence": "weekly",
    "interval": 1,
    "day_of_week": 4,                 // 0 = Mon, 4 = Fri
    "template_title": "Write the weekly post-mortem",
    "template_priority": "medium"
  }'
```

Cadence values are `daily`, `weekly`, `monthly`. `interval` is the
multiplier (every N days/weeks/months). For `weekly`, supply
`day_of_week` (0–6, Mon–Sun). For `monthly`, supply `day_of_month`
(1–31, clamped to the last day of short months — see
[ADR-0015](adr/0015-recurring-tasks.md)).

### Materialise due templates

The background worker calls this every minute. You can also call it
on demand:

```bash
curl -X POST http://localhost:8000/api/recurring-tasks/run
```

Each fired template produces a real `Task` row owned by the
synthetic *Recurring tasks* meeting. The audit chain records both
the new task ID and the next scheduled run.

## API key quotas

Per-key daily request limits, enforced with HTTP `429`.

### Set a limit

```bash
curl -X PUT http://localhost:8000/api/admin/quotas/42 \
  -H 'Content-Type: application/json' \
  -d '{"daily_limit": 1000}'
```

A key with no quota row is **unlimited** (back-compatible).

### Read live usage

```bash
curl http://localhost:8000/api/admin/quotas/usage
```

Returns a list sorted by request count desc, scoped to today (UTC).
Pass `?day=YYYY-MM-DD` to inspect a historic day.

### Per-key digest scheduling

While we're talking about per-key knobs, v0.12 also adds
`digest_hour_utc` to the notification preferences:

```bash
curl -X PUT http://localhost:8000/api/notifications/me \
  -d '{"digest_cadence": "daily", "digest_hour_utc": 9}'
```

Pass `digest_hour_utc: -1` to clear (revert to "any time"). The
sweeper that fans out digests reads this value via
`/api/notifications/digest-due?hour=N`.
