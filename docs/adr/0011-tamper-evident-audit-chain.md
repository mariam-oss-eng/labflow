# ADR-0011: Tamper-evident audit chain

* **Status**: accepted (v0.10)
* **Context**: regulated customers (research orgs, life-sciences,
  finance teams) need cryptographic proof that an audit log has not
  been silently rewritten. Stamping every row with `created_at` is
  insufficient — an attacker with DB write access can rewrite the row
  and the timestamp together.
* **Decision**: each row in `audit_events` carries a SHA-256 hash chain
  pointer. `entry_hash = sha256(prev_hash ‖ canonical(row payload))`,
  where `prev_hash` is the previous row's `entry_hash` for the same
  team. `/api/audit/verify` walks the chain and reports the first
  break.

## Why per-team chains, not one global chain?

Per-tenant chains keep one tenant's tampering from invalidating
another's history. They also avoid a global single-writer hotspot:
each chain is independent, so a 100-team installation can ingest
audit rows in 100 parallel sessions without serialising on a
"current head" row. The downside — operators cannot prove the order
of events *across* teams — is acceptable because cross-team ordering
is meaningless to a single-tenant auditor anyway.

## Why SHA-256 with `prev ‖ payload`?

* **SHA-256** is a NIST-blessed primitive every auditor recognises and
  every language has in stdlib. Faster choices (BLAKE3) bring no
  practical advantage at LabFlow's volume (≈10k events/day per team)
  and would be one more thing to defend in a compliance review.
* **`prev ‖ payload`** rather than full Merkle is sufficient when
  verification is sequential and serverside; we don't need
  range-proofs to external auditors yet. If we ever do, we can
  superimpose a Merkle tree on top — the leaves are already content-
  addressable.

## Backwards compatibility

Pre-v0.10 rows have NULL `prev_hash` / `entry_hash`. The verifier
treats a NULL row as a *legacy pivot*: it counts it under
`skipped_legacy`, resets the expected previous hash to the genesis
constant, and resumes verification on the next chained row. This
means an upgrade does **not** invalidate the chain — it just means
historical rows are not cryptographically proven, which matches the
operator's expectation.

## Canonicalisation gotcha

The first iteration hashed `created_at.isoformat()` with timezone
information attached. SQLite stores datetimes naively; on read-back
the timezone suffix vanished, so verification immediately failed for
every row. The canonicaliser now strips any `+TZ` / `Z` suffix
before hashing, so the round-tripped representation is what we sign.

## Consequences

* `audit_events` rows are now ~64 bytes wider (two hex-encoded SHA-256
  strings).
* Every `audit.record()` call performs one extra `SELECT … LIMIT 1` to
  fetch the previous row's hash. Indexed lookup; ~50µs.
* Operators who previously reordered audit rows manually (e.g. to fix
  a backfill bug) will now see a verification failure. The `tag`
  automation action correctly extends the chain instead of writing
  raw rows.
