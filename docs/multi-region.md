# Multi-region & time-travel (v0.9)

## Read-replica routing

Production deployments often have a primary write database plus one or
more read replicas. LabFlow 0.9 adds an opt-in router exposing
`read_session()` and `write_session()` context managers.

Configure replicas with a comma-separated list of SQLAlchemy URLs:

```bash
export LABFLOW_DATABASE_URL=postgresql+psycopg2://user@primary/labflow
export LABFLOW_READ_REPLICA_URLS=postgresql+psycopg2://user@replica1/labflow,postgresql+psycopg2://user@replica2/labflow
```

The router is **opt-in per call site**: existing endpoints continue to
hit the primary via `db.session_scope`. New read-only endpoints in v0.9
(`/api/timetravel/*`, `/api/copilot/sessions/{id}` GET, etc.) are good
candidates for migration.

`/readyz/replicas` returns per-replica health, suitable for wiring into
your existing readiness gate. See [ADR-0008](adr/0008-replica-routing.md)
for the design rationale and consistency model.

### Limitations

* No read-after-write consistency. Code that just wrote and needs to
  read its own write must use `write_session()` for both.
* No auto-failover. Use pgbouncer or HAProxy in front of replicas for
  HA; LabFlow itself doesn't (yet) auto-evict failing replicas.

## Time-travel queries

Reconstruct historical state of a meeting or task as of an arbitrary
timestamp by replaying the audit log:

```bash
curl 'http://localhost:8000/api/timetravel/tasks/42?as_of=2026-04-15T10:00:00Z'

# {
#   "id": 42, "title": "Provision GPU cluster",
#   "state": "in_progress", "status": "open",
#   "as_of": "2026-04-15T10:00:00Z",
#   "incomplete": false
# }
```

The `incomplete` flag is set when an audit action lacks the metadata
needed to fully rewind a field — the response is a best-effort
reconstruction in that case.

The lookback window is bounded by `LABFLOW_TIMETRAVEL_MAX_DAYS`
(default: 730 days). Future timestamps are rejected with HTTP 400.

## Data residency tags (preview)

The replica URLs are intentionally region-agnostic; the same router
will route by region tag in v1.0 once we land per-team residency
metadata. The current implementation is forward-compatible — operators
configuring replicas in one region today won't need to change anything
when the per-tenant routing lands.
