# ADR-0016: Per-API-key daily quotas

* **Status**: accepted (v0.12)
* **Context**: A single misbehaving integration can flood the API.
  Global rate limiting (v0.4) protects the process but punishes
  innocent callers. Operators want a per-key knob.
* **Decision**: introduce two narrow tables — `api_key_quotas`
  (declared limit) and `api_key_usage` (rolling per-day counter) —
  and a `increment_and_check` helper that hot routes call before
  doing real work.

## Schema choices

* **Quotas are explicit**, not derived. A key with no row is
  unlimited, which keeps the migration backwards-compatible (every
  existing key continues to work).
* **Usage is per-day, not a sliding window.** Daily counters are
  trivially correct under contention (one row per `(api_key_id, day)`
  with `UPDATE ... SET count = count + 1`); sliding windows need a
  per-request bucket which is real complexity to delete.
* **Counters live in the application DB**, not Redis. v0.12 deliberately
  avoids adding Redis as a hard dependency. If a deployment grows
  beyond the write throughput of its DB, the counter can move to
  Redis behind the same `quotas.increment_and_check` interface
  without touching any handler.

## Where the check fires

Quotas check at the API boundary (FastAPI dependency), *not* deep in
the service layer. This means:

* Background jobs (worker, scheduler, automation rules) are not
  metered — they don't have a calling API key.
* Webhooks fired by automations are not metered against the rule
  author's key.
* The same business logic invoked through the REPL is not metered.

If an operator wants to meter background traffic, they can add a
synthetic key per integration; the design supports it but doesn't
force it.

## Audit + observability

Every quota change emits a `quota.changed` audit event with the new
limit. Every `429` increments a `labflow_quota_exhausted_total{key}`
counter on the existing Prometheus surface. There is *no* per-request
log line for "request allowed by quota" — that would dominate the log
volume and gives no operational signal.

## Consequences

* Operators get a single dashboard (`/api/admin/quotas`) listing
  declared limits and live usage, sorted by recent traffic.
* Any handler can be metered by adding the dependency; no handler is
  metered by default (opt-in protects against accidental breakage of
  health checks and read-only endpoints).
* Migrating to Redis later is a one-file change.
