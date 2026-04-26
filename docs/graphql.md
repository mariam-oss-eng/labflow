# GraphQL

LabFlow ships a read-only GraphQL endpoint at `POST /graphql`. The
endpoint shares the REST API's authentication, RBAC, rate-limiting, and
audit middleware — every query is team-scoped automatically.

## Quickstart

```bash
curl -sS https://labflow.example.com/graphql \
  -H "Authorization: Bearer $LABFLOW_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "{ tasks(status: \"open\", limit: 5) { id title owner due_date } }"}'
```

Or via the Python SDK:

```python
from labflow_client import LabFlow
lf = LabFlow("https://labflow.example.com", api_key="lfk_...")
result = lf.graphql("""
  query Dashboard {
    team { slug }
    analytics(days: 30) { tasks_closed cycle_time_p50_days completion_rate }
    decisions(limit: 5) { id statement confidence }
  }
""")
```

## Schema

| Field | Args | Returns |
| --- | --- | --- |
| `team` | _none_ | `{ id, slug, name }` |
| `tasks` | `status: String, limit: Int` | `[Task]` (max 200) |
| `decisions` | `limit: Int` | `[Decision]` ordered newest-first |
| `meetings` | `limit: Int` | `[Meeting]` ordered newest-first |
| `comments` | `entity_type: String!, entity_id: Int!` | `[Comment]` (chronological) |
| `analytics` | `days: Int` | `Analytics` (same shape as REST `/api/analytics`) |
| `__schema` | _none_ | Introspection root |

`Task`, `Decision`, `Meeting`, `Comment`, and `Analytics` types match
their REST equivalents one-to-one.

## Errors

The endpoint always returns HTTP 200 (per the GraphQL convention).
Field-level failures appear in the `errors` array:

```json
{
  "data": { "tasks": [] },
  "errors": [{ "message": "unknown field: 'bogus'" }]
}
```

## Limitations

This is a hand-rolled, dependency-free implementation (see [ADR-0006](
adr/0006-hand-rolled-graphql.md)). It deliberately does **not** support
mutations, fragments, variables, directives, unions, interfaces, or
subscriptions. Use REST for writes and the WebSocket channel for
realtime updates.
