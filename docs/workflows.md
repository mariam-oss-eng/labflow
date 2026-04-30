# Workflows & Sprints (v0.8)

LabFlow ships a per-team **state-machine engine** for tasks plus
time-boxed **sprints** that group tasks for planning and burndown.

## Workflow definition

A workflow is JSON with four keys:

```json
{
  "states": ["open", "in_progress", "in_review", "closed", "wont_fix"],
  "initial": "open",
  "terminal": ["closed", "wont_fix"],
  "transitions": [
    {"from": "open", "to": "in_progress"},
    {"from": "in_progress", "to": "in_review"},
    {"from": "in_review", "to": "closed", "sla_hours": 24},
    {"from": "any", "to": "closed", "role": "admin"}
  ]
}
```

* **`from: "any"`** matches any source state.
* **`role`** restricts a transition to actors with at least that RBAC
  role (`admin > member > read`).
* **`sla_hours`** sets `tasks.sla_breach_at = now + sla_hours` when the
  transition fires; the periodic `POST /api/admin/sla/sweep` finds rows
  past their deadline and emits `task.sla.breach` events.

A default workflow is auto-created on first call to
`GET /api/workflows`. Custom workflows are POSTed and may be marked
`make_default: true`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/api/workflows` | List + auto-create default |
| `POST` | `/api/workflows` | Create (admin) |
| `POST` | `/api/tasks/{id}/transition` | Take a transition |
| `POST` | `/api/admin/sla/sweep` | Emit breach events |

Every transition is audit-logged (`task.transition`) and broadcast on
SSE / WebSocket.

## Sprints

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/api/sprints` | List (optionally `?active=true`) |
| `POST` | `/api/sprints` | Create (slug auto-derived from name) |
| `POST` | `/api/sprints/{slug}/assign` | Add a task |
| `POST` | `/api/sprints/{slug}/close` | Mark inactive |
| `GET`  | `/api/sprints/{slug}/burndown` | Daily remaining-task counts |

The burndown is reconstructed deterministically from `audit_events`, so
it's an exact replay rather than a snapshot — there's no nightly
denormalisation step to keep in sync.

## Critical path

`POST /api/tasks/{id}/depends_on` records a directed dependency. Cycles
are rejected on insert. `GET /api/tasks/critical-path` returns the
longest weighted path through the team's open task DAG using a
Kahn-topo + longest-path DP; the weight of each node is `(1 -
confidence) + 1` so low-confidence tasks pull the path slightly toward
themselves (proxy for "this is risky, watch it").

See ADRs 0007 (vector v2 — search infra used by the same dashboard) and
the test suite at `tests/test_v08.py` for end-to-end examples.
