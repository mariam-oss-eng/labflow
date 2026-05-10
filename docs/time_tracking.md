# Time tracking & effort estimates · v0.14

LabFlow tracks how long a task actually takes. Two flavours of entry
share one row (`time_entries.source = 'timer' | 'manual'`):

* a **timer** — start, stop later
* a **manual** entry — log a closed range after the fact

## Endpoints

```
POST   /api/tasks/{id}/time/start    {owner_id?, note?}     → {id, started_at, ...}
POST   /api/tasks/time/stop          {owner_id?}            → {id, ended_at, ...}
POST   /api/tasks/{id}/time          {started_at, ended_at, owner_id?, note?}
GET    /api/tasks/{id}/time                                  → totals + per-owner
GET    /api/time/report?days=14                              → team roll-up
PUT    /api/tasks/{id}/effort        {effort_hours}          → set/unset estimate
```

## The single-open-timer invariant

Each `(team, owner)` may have **at most one open timer at a time**.
Calling `start` while a timer is already running stops it implicitly
at the new timer's start time and emits a separate
`time.timer_stopped_implicit` audit event. This matches "I switched
tasks" semantics without requiring two round-trips.

## Reports

`GET /api/tasks/{id}/time` returns:

```json
{
  "task_id": 42,
  "total_seconds": 7320,
  "total_hours": 2.033,
  "open_timer_count": 0,
  "by_owner": [{"owner_id": 5, "seconds": 7320}]
}
```

`GET /api/time/report?days=N` returns per-task and per-owner totals
across the team for the window.

## Effort estimates

`tasks.effort_hours` is a nullable `Float`. When set, it overrides the
heuristic in `dag.py` so the critical-path calculation uses real
numbers. `PUT /api/tasks/{id}/effort` accepts `{effort_hours: <0..10000 | null>}`.

Audit actions emitted: `time.timer_started`, `time.timer_stopped`,
`time.timer_stopped_implicit`, `time.logged`. All entries land in the
v0.10 hash chain.

See [ADR-0019](adr/0019-time-tracking.md) for design notes.
