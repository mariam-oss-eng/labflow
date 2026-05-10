# Public share links · v0.15

Mint a time-bound, revocable, **read-only** URL pointing at one
decision, task, or wiki page — without granting an account or API key.

## Workflow

```bash
# 1. Mint (admin role required) — token is returned exactly once.
curl -X POST $LF/api/shares -H "X-LabFlow-Key: $KEY" -d '{
  "entity_type": "decision",
  "entity_id": 42,
  "ttl_hours": 168
}'
# → {"id": 7, "token": "lfshare_...", "expires_at": "2026-05-17T..."}

# 2. Share the URL — no auth needed.
open "$LF/share/lfshare_..."

# 3. Revoke when done.
curl -X DELETE $LF/api/shares/7 -H "X-LabFlow-Key: $KEY"
```

## Token model

* 32-byte URL-safe random + `lfshare_` prefix → returned to the
  creator **once**.
* Persisted as SHA-256 of the plaintext (`auth.hash_api_key` rationale
  applies — see [ADR-0022](adr/0022-public-shares.md)).
* `expires_at` is mandatory (default 7 days, max 90 days).
* `view_count` increments on every successful resolve so admins can
  spot abandoned-but-active links.

## Shape of a resolved share

```json
{
  "share": {"entity_type": "decision",
            "expires_at": "2026-05-17T08:00:00"},
  "data":  {"type": "decision", "id": 42,
            "statement": "Adopt RFC-7", "rationale": "..."}
}
```

The render function only includes the named entity's fields — never
team metadata, never the audit chain.

## What public shares are *not*

* They're not write tokens — `/share/{token}` is GET-only.
* They're not API keys — they can't hit `/api/...`.
* They don't survive the entity being deleted — a missing target
  produces a 404.
