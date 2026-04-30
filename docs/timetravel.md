# Time-travel queries (v0.9)

Reconstruct the historical state of a meeting or task as of any past
timestamp by replaying the `audit_events` log in reverse.

## Endpoints

| Method | Path | Returns |
|---|---|---|
| `GET` | `/api/timetravel/tasks/{task_id}?as_of=ISO`     | Task state as of `as_of` |
| `GET` | `/api/timetravel/meetings/{meeting_id}?as_of=ISO` | Meeting + decisions + tasks as of `as_of` |

## Example

```bash
curl 'http://localhost:8000/api/timetravel/tasks/42?as_of=2026-04-15T10:00:00Z'
```

```json
{
  "id": 42, "title": "Provision GPU cluster",
  "state": "in_progress", "status": "open",
  "owner_id": 7, "due_date": "2026-04-30T00:00:00",
  "as_of": "2026-04-15T10:00:00Z",
  "incomplete": false
}
```

## Window

`as_of` must be:

* Non-future (rejected with HTTP 400 otherwise).
* Within `LABFLOW_TIMETRAVEL_MAX_DAYS` of now (default: 730 days).

## `incomplete: true`

The replay can only rewind fields whose audit events recorded the
previous value (`task.transition` records `from`/`to`, for example).
For other fields we return the *current* row value and set
`incomplete: true` so callers know the snapshot is best-effort.

The transcript on a meeting is immutable post-finalize, so meetings
never report `incomplete` for transcript-derived fields.
