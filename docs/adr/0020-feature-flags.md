# ADR-0020: Per-team feature flags, no third-party service

* **Status**: accepted (v0.14)
* **Context**: We need to dark-launch features (e.g. a new copilot
  variant) and gate experimental endpoints. Buying LaunchDarkly or
  Statsig solves the same problem at the cost of (a) a runtime SaaS
  dependency, (b) a per-MAU bill, and (c) a network hop on every
  flag check.
* **Decision**: Ship a `feature_flags` table — `(team_id, key,
  enabled, payload_json)` — and read it through a 1-second LRU-style
  process cache. Admins flip flags via `PUT /api/feature-flags/{key}`.

## What's in the table

* `key` — `[a-z0-9._-]{1..80}`, validated on every write. Forces flag
  names like `copilot.beta` or `mcp.read_write` rather than
  free-text scribbles that drift across environments.
* `enabled` — boolean. The most common shape; cheap to read.
* `payload_json` — optional JSON object. Lets a flag carry parameters
  (`{"variant": "B", "bucket": 0.5}`) without inventing a separate
  config service.

## Why a 1-second cache

Flag reads happen on hot paths. A round-trip to Postgres on every
request would matter. A 1s TTL means an admin flipping a flag sees
their change propagate within a second across the whole cluster — fast
enough for "turn this off, things are on fire" without making the
hot path stupid.

## What we don't do

* **No percentage rollouts**. Rolled out percentages can be encoded in
  the JSON payload and evaluated client-side. Putting that logic in
  the table forces a complete rewrite when you need a cohort that's
  not "X% of users".
* **No targeting rules**. The flag is per-team. Per-user gating lives
  in the `payload_json` if you want it.
