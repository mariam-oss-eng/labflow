# Boards & smart lists

Two complementary planning surfaces, both added in v0.12 / v0.13.

## Kanban board (v0.12)

```
GET  /api/board/{workflow_slug}      # JSON columns + cards
GET  /app/board/{workflow_slug}      # rendered HTML page
```

`workflow_slug` is the workflow's `name` (workflows are name-addressed
since v0.8). Use `_default` to pick the team's default workflow:

```bash
curl http://localhost:8000/api/board/_default
```

The response shape:

```json
{
  "workflow": {"id": 1, "name": "default"},
  "columns": [
    {"state": "open", "name": "Open",        "kind": "open",
     "tasks": [{"id": 12, "title": "...", ...}], "count": 1},
    {"state": "in_progress", "name": "In progress", "kind": "open",
     "tasks": [], "count": 0},
    ...
  ],
  "total": 1
}
```

Tasks not yet in any of the workflow's states (e.g. legacy rows
created before the workflow existed) fall into a synthetic `inbox`
column so they remain visible.

The HTML page is a single self-contained document — no JS framework,
no external CSS — that renders columns side by side with cards
showing title, owner, due date, and the task ID.

## Smart lists (v0.13)

A smart list is a slug-addressed JSON filter:

```bash
curl -X PUT http://localhost:8000/api/smart-lists/my-week \
  -H 'Content-Type: application/json' \
  -d '{"name": "My week",
       "filter": {"assignee_handle": "alice",
                  "state": "in_progress",
                  "due_before": "2026-06-01"}}'
```

Then run it:

```bash
curl http://localhost:8000/api/smart-lists/my-week/run
```

### Supported filter keys

All keys are optional. Missing keys are wildcards. Unknown keys are
rejected at save time so a typo can never silently match everything.

| Key | Type | Match |
| --- | --- | --- |
| `state`            | string | exact `Task.state` |
| `status`           | string | exact `Task.status` |
| `assignee_handle`  | string | case-insensitive `Owner.handle` |
| `priority`         | `low\|medium\|high` | exact `Task.priority` |
| `label`            | string | substring of `Task.title` |
| `due_before`       | ISO date | `Task.due_date <= …` |
| `sprint_slug`      | string | exact `Sprint.slug` |

### Why a closed schema?

See [ADR-0018](adr/0018-smart-lists.md) for the design discussion.
The short version: a flat AND-of-equality model covers every shipped
filter so far, has no parser to fuzz, and degrades cleanly into
`{"clauses": [...]}` if we ever need disjunction.
