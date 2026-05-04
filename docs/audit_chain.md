# Audit chain & verification

LabFlow's audit log (`audit_events`) is **append-only** *and*
**tamper-evident** as of v0.10. Every row carries a SHA-256 hash that
chains back to the previous row for the same team, so any silent
rewrite of historical data is detectable in O(rows) time.

## Why operators care

* **Compliance** — auditors want a cryptographic guarantee that the
  log they're reading is the log that was written.
* **Insider risk** — even a privileged operator with database access
  cannot rewrite history without breaking the chain.
* **Forensics** — if something does break, `verify_chain` reports
  *exactly* the first row that diverged.

## Verifying the chain

```bash
curl -H "X-API-Key: $KEY" https://labflow.example/api/audit/verify
```

```json
{
  "ok": true,
  "checked": 12047,
  "first_break_id": null,
  "reason": null,
  "skipped_legacy": 0
}
```

A failure looks like:

```json
{
  "ok": false,
  "checked": 4291,
  "first_break_id": 4292,
  "reason": "entry_hash mismatch at id=4292",
  "skipped_legacy": 0
}
```

Treat any non-`ok` response as an incident: investigate the
`first_break_id` row plus everything after, and restore from your most
recent verified backup if needed (see [Backup & restore](backup.md)).

## How the chain is computed

For each new audit row:

```
prev_hash  = previous row's entry_hash for the same team (or 64 zeros)
payload    = canonical_json({team_id, actor, action, entity_type,
                             entity_id, metadata_json, created_at})
entry_hash = sha256(prev_hash || "\n" || payload)
```

* Both fields are 64 hex chars (SHA-256 output).
* `payload` strips any timezone suffix from `created_at` so the
  chain survives SQLite's naive-datetime round-trip — see
  [ADR-0011](adr/0011-tamper-evident-audit-chain.md).
* The chain is **per team**, not global, so one tenant's tampering
  cannot poison another tenant's history.

## Legacy rows

Rows written before v0.10 have NULL `prev_hash` / `entry_hash`. The
verifier counts these under `skipped_legacy` and re-anchors at the
next chained row. This means upgrading from v0.9 → v0.10 does **not**
invalidate your existing log; it just means historical rows stay
unverifiable until you write new ones.

## What about deletion?

LabFlow never deletes audit rows. The team-erase endpoint
(`POST /api/admin/erase`) writes a `team.erased` marker row, then
removes data tables — the audit log itself is the proof that the
erasure happened.
