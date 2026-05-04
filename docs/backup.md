# Backup & restore

LabFlow ships a **signed JSON backup** primitive (v0.10) so operators
can take a full team snapshot, store it in their own object storage,
and restore into a fresh team without depending on a vendor backup
service.

## Why JSON, not pg_dump?

* **Portability** — the same backup restores into SQLite or Postgres.
* **Auditability** — the backup is a single signed document; an
  auditor can `cat` it.
* **Compositional encryption** — pipe the signed envelope through
  `age` / `gpg` / your KMS for at-rest confidentiality. We
  intentionally avoid baking in a half-baked encryption layer.

## Taking a backup

```bash
SECRET=$(openssl rand -hex 32)
curl -s -X POST -H "X-API-Key: $KEY" \
     "https://labflow.example/api/admin/backup?secret=$SECRET" \
     | tee labflow-backup-$(date +%F).json | jq '.header'
```

Response shape:

```json
{
  "header": {
    "schema_version": "1",
    "created_at": "2026-05-04T22:00:00",
    "team_slug": "default",
    "row_count": 1827
  },
  "payload": {"team": {...}, "tables": {...}},
  "signature": "9f8b…"
}
```

The signature is `HMAC-SHA256(secret, canonical(header) +
canonical(payload))`. **Keep `$SECRET` outside the backup file** —
ideally in your secrets manager. Without it, the envelope cannot be
verified or restored.

## Previewing a restore

```bash
curl -s -X POST -H "Content-Type: application/json" -H "X-API-Key: $KEY" \
     -d "{\"envelope\": $(cat labflow-backup-2026-05-04.json), \
          \"secret\": \"$SECRET\"}" \
     https://labflow.example/api/admin/restore/preview
```

The preview verifies the signature and returns row counts. It does
not write to the database.

## Applying a restore

Restore always lands in a **new team slug** — never into an existing
team. This is intentional: a restore is a recovery operation, and
silently overwriting live data is the worst possible default. To
"swap in" a restored team, change the calling key's team membership
after the restore completes.

```bash
curl -s -X POST -H "Content-Type: application/json" -H "X-API-Key: $KEY" \
     -d "{\"envelope\": $(cat …json), \"secret\": \"$SECRET\", \
          \"new_slug\": \"default-restored-2026-05-04\"}" \
     https://labflow.example/api/admin/restore/apply
```

The response carries per-table insert counts:

```json
{"team_id": 27, "team_slug": "default-restored-2026-05-04",
 "inserted": {"meetings": 412, "tasks": 1093, ...}}
```

## What's *not* restored

* `api_keys` — restores must explicitly re-issue keys for the new
  team.
* `audit_events.entry_hash` — the new team's audit chain re-anchors
  at restore. The restored data is logged via a single
  `admin.restore` audit row.
* `alembic_version`, `jobs`, internal state tables.

This is by design: a restore is a recovery, not a clone.
