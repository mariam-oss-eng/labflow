# Forecasting & dashboards

LabFlow ships two complementary visibility primitives in v0.10:

* **`/api/forecast/...`** — directional sprint and per-task ETAs.
* **`/api/dashboards/...`** — per-key customisable widget layouts.

## Sprint forecast

```bash
curl -H "X-API-Key: $KEY" \
     https://labflow.example/api/forecast/sprint/sprint-3
```

```json
{
  "sprint": {"slug": "sprint-3", "task_count": 18, ...},
  "method": "linear_regression",
  "samples": 6,
  "slope_per_day": -1.83,
  "remaining_today": 11,
  "eta_iso": "2026-05-12",
  "eta_within_sprint": true,
  "confidence_days": 1.4,
  "warning": null
}
```

Under the hood it's a least-squares fit on the burndown's `remaining`
series — projecting forward to find the day the line crosses zero.
`confidence_days` is a ±1σ band proportional to the residual variance.

When the burndown is flat or rising, no ETA is returned and `warning`
explains why.

## Per-task ETA

```bash
curl https://labflow.example/api/forecast/task/4127
```

```json
{
  "task_id": 4127, "owner_id": 12, "method": "median_cycle_time",
  "samples": 9, "median_hours": 18.4,
  "eta_iso": "2026-05-06T09:12:33", "already_closed": false
}
```

The estimator uses the owner's median historical cycle time (closed
`Task.closed_at - Task.created_at`). When fewer than 3 samples are
available, it falls back to the team-wide median (`team_fallback`).
With no history at all, you get `method: "no_history"` and a null
ETA — better than a fake number.

## Dashboards

A dashboard is a *list of widget specs*. Specs are JSON; the server
owns the widget catalogue (so the client can stay dumb).

```bash
# What widgets exist?
curl https://labflow.example/api/dashboards/widgets

# Save a dashboard
curl -X POST -H "Content-Type: application/json" -H "X-API-Key: $KEY" \
     -d '{"slug":"home","name":"Home","layout":[
            {"kind":"open_tasks","params":{"limit":10}},
            {"kind":"sprint_burndown","params":{"slug":"sprint-3"}},
            {"kind":"recent_decisions","params":{"limit":5}}]}' \
     https://labflow.example/api/dashboards

# Render a dashboard (data for every widget in one call)
curl https://labflow.example/api/dashboards/home/data
```

Built-in widgets:

| kind                  | summary                                              |
|-----------------------|------------------------------------------------------|
| `open_tasks`          | non-closed tasks, most-recent first                  |
| `recent_decisions`    | most recent decisions                                |
| `sla_breaches`        | tasks past their `sla_breach_at` and not closed      |
| `sprint_burndown`     | burndown series for the requested sprint slug        |
| `automation_status`   | rule counts + total fires this team                  |

Add widgets by registering a function with `@dashboards.register("name")`
that returns a JSON-serialisable dict.
