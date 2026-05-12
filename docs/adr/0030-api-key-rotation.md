# ADR-0030: API key rotation with a configurable grace window

* **Status**: accepted (v0.17)
* **Context**: Rotating an API key without server support means
  downtime: clients have to swap to the new key the moment the old
  one is revoked. That's incompatible with phased rollouts, mobile
  apps, IoT devices, or anything else where "all clients update
  simultaneously" is a fantasy.
* **Decision**: Add `api_keys.rotated_from_id` (FK back to the
  predecessor) and `api_keys.rotation_grace_until`. `rotate()` mints
  a successor and stamps the predecessor's grace timestamp; both
  keys are valid in parallel during the grace window. A sweeper
  `sweep_expired()` revokes any key whose grace timestamp has
  elapsed.

## Why a column on the existing table

* Auth lookup is already on the hot path (`WHERE key_hash = ? AND
  revoked_at IS NULL`); adding a join would be a regression.
* The grace window is just data on the *old* row — the auth path
  doesn't even need to look at it. The sweeper is the only thing
  that does.

## Sweeper, not lazy expiry

We don't filter `revoked_at IS NULL OR rotation_grace_until > now()`
in the auth dependency. Two reasons:

1. The auth path stays as-is — `revoked_at IS NULL` already excludes
   keys the sweeper has retired.
2. Expiring keys on the read path would mean every request can mutate
   state. The sweeper centralises that concern.

If the sweeper falls behind, the old key keeps working a bit longer
than the grace window — a *fail-open* behaviour we explicitly prefer
over locking customers out. Operators can run the sweeper on demand
via `POST /api/keys/_sweep-expired`.

## What we deferred

* Multi-key rotation chains (rotate B from A, then C from B). The
  current model rejects double-rotation (`ConflictError`); operators
  cancel first, then rotate. Chained rotation has no real-world
  use case yet.
* Webhook on rotation events. The audit log carries
  `api_key.rotate{,.expire,.cancel}` and clients who want a webhook
  can subscribe via the standard mechanism.
