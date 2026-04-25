# REST API

The full machine-readable schema lives at `/openapi.json` on a running
instance, and Swagger UI is mounted at `/docs`. This page is the
human-friendly companion.

## Authentication

* **Single-team mode** (`LABFLOW_AUTH_ENABLED=false`, the default) —
  no header required; every request maps to the `default` team.
* **Multi-tenant mode** (`LABFLOW_AUTH_ENABLED=true`) — pass
  `Authorization: Bearer <key>` (or `X-LabFlow-Key: <key>`). Keys are
  hashed at rest; see the operations runbook for rotation.

Every request also gets:

| Header | Notes |
| --- | --- |
| `X-Request-Id` | echoed back; auto-generated when not provided |
| `X-Response-Time-Ms` | server-side wall-clock (informational) |
| `X-RateLimit-Limit` / `X-RateLimit-Remaining` | when rate-limit is enabled |
| `Idempotent-Replay: true` | on a cached response replay |

## Errors

Every error returns a stable envelope:

```json
{
  "error": {
    "code": "validation_error",
    "message": "transcript + notes exceed size limit",
    "details": null,
    "request_id": "8f1a…"
  }
}
```

Codes: `validation_error`, `not_found`, `conflict`, `rate_limited`,
`idempotency_mismatch`, `payload_too_large`, `forbidden`,
`internal_error`.

## Endpoints

### Meetings

| Method | Path | Body | Notes |
| --- | --- | --- | --- |
| `POST` | `/api/meetings` | `{title, transcript, notes?, meeting_type?}` | Auto-extracts on create |
| `POST` | `/api/meetings/upload` | multipart | File upload with title + type |
| `POST` | `/api/meetings/{id}/extract` | — | Re-run extraction |
| `POST` | `/api/meetings/{id}/extract:async` | — | Returns `202` + job id |
| `POST` | `/api/meetings/{id}/finalize` | — | Locks the meeting, emits `meeting.finalized` |
| `GET` | `/api/meetings` | `?limit=&offset=` | Paginated list |
| `GET` | `/api/meetings/{id}/export.json` | — | Full meeting + extractions |
| `GET` | `/api/meetings/{id}/export.md` | — | Markdown summary |

### Tasks & evidence

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/api/tasks` | filter by `?status=&owner=` |
| `PATCH` | `/api/tasks/{id}` | `{status?, owner_id?, due_date?}` |
| `POST` | `/api/evidence` | `{task_id, kind, uri, summary?}` |

### Search & graph

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/api/search` | `?q=&alpha=&limit=`; returns `score_components` |
| `GET` | `/api/graph/decisions` | `{nodes, edges}` |
| `GET` | `/api/graph/decisions.mermaid` | Rendered Mermaid `flowchart` |

### System & live

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/api/me` | Caller identity, role, posture |
| `GET` | `/api/stream` | `text/event-stream`, heartbeat every 15s |
| `GET` | `/healthz` | Liveness |
| `GET` | `/readyz` | Readiness (round-trips DB) |
| `GET` | `/metrics` | Prometheus exposition |

### Admin (role: `admin`)

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/api/admin/export` | GDPR Article 15 |
| `DELETE` | `/api/admin/erase` | GDPR Article 17 |
| `POST` | `/api/admin/retention/sweep` | Run the retention sweep on demand |
