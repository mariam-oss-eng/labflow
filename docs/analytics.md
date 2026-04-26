# Analytics

LabFlow's typed graph makes operational metrics computable in one SQL
pass per metric — no external BI tool required. The endpoint is
`GET /api/analytics?days=N` (default 30).

## Response shape

```json
{
  "period_start": "2026-03-27T10:56:22",
  "period_end":   "2026-04-26T10:56:22",
  "meetings": 14,
  "finalized_meetings": 11,
  "decisions": 23,
  "tasks_opened": 47,
  "tasks_closed": 39,
  "cycle_time_p50_days": 2.4,
  "cycle_time_p90_days": 9.1,
  "completion_rate": 0.83,
  "blocker_rate":    0.18,
  "top_owners": [
    {"handle": "alice", "closed": 12},
    {"handle": "bob",   "closed":  9}
  ],
  "weekly_trend": [
    {"week_start": "2026-03-02", "opened": 5, "closed":  3},
    {"week_start": "2026-03-09", "opened": 7, "closed":  6},
    ...
  ]
}
```

## Definitions

* **`cycle_time_pXX_days`** — the percentile of `(closed_at - created_at)`
  in days, over tasks closed in the window. `null` if no closures.
* **`completion_rate`** — `tasks_closed / tasks_opened`, both within the
  window. Useful as a rolling productivity signal even when teams change.
* **`blocker_rate`** — `open_blockers / finalized_meetings`. >1.0 means
  meetings are surfacing more blockers than they resolve.
* **`top_owners`** — the five handles with the most closed tasks in the
  window. Driven by the same ownership data the digest uses.
* **`weekly_trend`** — a fixed-length 8-element array of
  `{week_start, opened, closed}` covering the most recent ISO weeks. The
  dashboard renders it as a sparkline.

## Performance

Each metric is a single aggregate query against an indexed column
(`team_id`, `closed_at`, `created_at`). The endpoint runs comfortably
under 100 ms for a backlog of hundreds of thousands of tasks.

## GraphQL

The same payload is exposed as `analytics(days: Int)` in GraphQL — see
[GraphQL](graphql.md) for selection-set examples.
