# ADR-0008 — Read-replica routing

**Status:** Accepted (v0.9, 2026-04-30)

## Context

Production LabFlow deployments are read-heavy: dashboards poll
`/api/analytics`, `/api/search`, and `/api/graph` on every page view,
while writes (a meeting upload here, a transition there) are
comparatively rare. With everything pointed at a single primary, the
`/api/search` and `/api/analytics` endpoints noisy-neighbour the OLTP
write path under load.

We want operators to be able to fan out reads across one or more
PostgreSQL streaming replicas without touching application code, while
keeping the local-dev SQLite story (single file, one connection) intact.

## Decision

Add a `labflow.replica` module that exposes two context managers:

* `write_session()` — yields a session bound to the primary
  (`LABFLOW_DATABASE_URL`); commits on success.
* `read_session()` — yields a *read-only* session bound to the next
  replica in a round-robin over `LABFLOW_READ_REPLICA_URLS`. Always
  rolls back on exit; falls back to the primary when the env var is
  empty.

The router is **opt-in per call site** — existing endpoints keep using
`labflow.db.session_scope`, so this is a non-breaking change. New
read-only endpoints (and v0.9 routes like `/api/timetravel/*` and
`/api/copilot/sessions/{id}` GET) can opt in to `read_session()`.

A new `/readyz/replicas` endpoint runs `SELECT 1` against every replica
and returns a per-replica health row, so operators can wire this into
their existing readiness gate.

## Consequences

* **No magic, no breaking changes.** `db.session_scope` keeps doing what
  it did. Migration of an endpoint to `read_session()` is a code change
  reviewers can audit.
* **No read-after-write consistency.** A caller that just wrote and
  needs to read its own write must use `write_session()` for both. We
  considered threading a "prefer-primary" hint via `Request.state` but
  decided to defer until we have a concrete use case.
* **Replica failover is the operator's job.** When a replica is
  unhealthy, requests routed to it will fail; we don't (yet) auto-evict
  failing replicas from the round-robin. `/readyz/replicas` makes the
  state observable; pgbouncer or HAProxy in front of replicas is the
  recommended HA story.
* **SQLite tests continue to work.** With no replicas configured,
  `read_session()` just returns a primary session; the test suite
  doesn't need a Postgres cluster.

## Alternatives considered

| Option | Why we passed |
|---|---|
| **SQLAlchemy `routing_session_class`** | Bigger surface area, harder to reason about; we wanted explicit `read_session()` call sites for grep-ability |
| **pgbouncer/HAProxy only, no app router** | Works, but the app then doesn't know which connection is read-only and can't make safe choices like always rolling back on exit |
| **Async always** | Out of scope for this release; would require a much larger refactor of the FastAPI dependency tree |

## See also
* `labflow/replica.py`
* `labflow/main.py` — `/readyz/replicas`
* `tests/test_v09.py::test_replica_*`
