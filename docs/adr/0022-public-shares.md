# ADR-0022: Public share links — SHA-256 tokens, time-bound, revocable

* **Status**: accepted (v0.15)
* **Context**: A user wants to share *one* decision or task with
  someone outside the team, without granting them an account or an
  API key. The natural primitive is a tokenised URL.
* **Decision**: A `public_shares` row holds the SHA-256 of a 32-byte
  random token. Plaintext is returned to the creator exactly once.
  Resolution hashes the incoming token and looks it up by hash.
  Every share has a hard `expires_at` and a soft `revoked_at`.

## Why SHA-256 (not bcrypt/argon2/scrypt)

Same rationale as `auth.hash_api_key`: a 256-bit machine-generated
random token is computationally infeasible to brute-force against
SHA-256 regardless of how fast SHA-256 is. The slow KDFs solve a
different problem (low-entropy user-chosen passwords). On the hot
path of a public share resolve, a slow KDF would add real latency for
zero real-world security benefit. CodeQL's
`py/weak-sensitive-data-hashing` rule does not apply.

## What a share can point at

`decision`, `task`, or `wiki` — the three entities a research team
typically wants to publish. Adding a new entity type is one entry in
`VALID_ENTITIES` plus a branch in `_render`.

## What a share never carries

* No write access. `/share/{token}` is GET-only.
* No team-wide data. The render function only includes fields of the
  named entity.
* No raw audit chain or other team metadata.

## Lifecycle

* **Create**: `POST /api/shares` (admin role) → returns token + id.
* **List**:   `GET  /api/shares` → never includes the plaintext.
* **Revoke**: `DELETE /api/shares/{id}`.
* **Use**:    `GET /share/{token}` (no auth) — fails with 404 if
              expired, revoked, or unknown.

`view_count` increments on every successful resolve, so an admin can
see at a glance whether a share is being used.
