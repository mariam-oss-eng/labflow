# LFQL, custom fields & scheduled reports (v0.16)

v0.16 is the **power-user release**. It adds a small query language,
arbitrary per-team metadata, and a scheduler that fires queries at a
cadence — three building blocks that together let you replace a lot
of bespoke scripts.

## At a glance

| Feature | Module | Endpoint |
|---|---|---|
| LabFlow Query Language (LFQL) | `labflow.lfql` | `GET /api/lfql/{explain,run}` |
| Custom fields | `labflow.custom_fields` | `POST/GET/DELETE /api/custom-fields`, `PUT/GET/DELETE /api/{entity_type}/{id}/fields/...` |
| Scheduled reports | `labflow.scheduled_reports` | `POST/GET/DELETE /api/reports`, `POST /api/reports/_run-due` |

## LFQL — boolean filters in one line

```text
status:open AND owner:alice
priority:>=high AND title:"login bug"
NOT status:done AND created:>2026-01-01
```

Supported keys: `status`, `state`, `owner`, `title`, `kind`,
`priority`, `due`, `created`, `effort`. Operators: `:`, `:>`, `:<`,
`:>=`, `:<=`, `:!=`. `:` on a string is **substring, case-insensitive**;
on numbers it's equality.

```bash
curl -s 'http://localhost:8000/api/lfql/explain?q=status:open%20AND%20owner:alice' | jq
{
  "query": "status:open AND owner:alice",
  "ast": { "and": [
    { "atom": { "key": "status", "op": ":", "value": "open" } },
    { "atom": { "key": "owner",  "op": ":", "value": "alice" } }
  ]}
}

curl -s 'http://localhost:8000/api/lfql/run?q=priority:%3E%3Dhigh' | jq '.matched'
3
```

LFQL parses **before** any DB call, so a typo in a field name
returns 400 immediately rather than silently matching everything.

See [ADR-0024 — LFQL](adr/0024-lfql.md).

## Custom fields

Define a field once, attach values to many entities. Four kinds:
`text`, `number`, `date`, `select`.

```bash
# 1. Define the field (admin)
curl -X POST localhost:8000/api/custom-fields -d '{
  "entity_type": "task", "key": "epic", "label": "Epic",
  "kind": "select", "options": ["A", "B", "C"]
}'

# 2. Attach a value
curl -X PUT localhost:8000/api/task/42/fields/epic -d '{"value": "B"}'

# 3. Read all values for one task
curl -s localhost:8000/api/task/42/fields | jq
{ "values": { "epic": "B" } }
```

Validation is per-kind: numbers coerce to float, dates must parse as
ISO-8601, `select` rejects values outside `options`.

See [ADR-0025 — custom fields](adr/0025-custom-fields.md).

## Scheduled reports

A report = `(name, LFQL query, cadence, webhook_url)`. The sweeper
runs due reports, POSTs the matching task IDs (HMAC-signed if you set
a `secret`), and records every run.

```bash
curl -X POST localhost:8000/api/reports -d '{
  "name": "open-bugs-daily",
  "query": "status:open AND title:bug",
  "cadence": "daily",
  "webhook_url": "https://hooks.slack.com/services/...",
  "secret": "shared-secret"
}'

# Operator nudge — run anything overdue right now.
curl -X POST localhost:8000/api/reports/_run-due
{ "executed": 1 }

# Inspect history.
curl -s localhost:8000/api/reports/1/runs | jq
```

Cadences: `hourly`, `daily`, `weekly`. Bodies are signed with
`X-LabFlow-Signature-256: sha256=<hex>` exactly like normal webhooks.

See [ADR-0026 — scheduled reports](adr/0026-scheduled-reports.md).

## Operations

* The reports sweeper is operator-driven (`POST /api/reports/_run-due`).
  Wire it to your favourite cron (every 5 minutes is plenty).
* Custom field deletes cascade to values. Audit rows survive.
* LFQL has no global rate limit on top of the standard API limit;
  every query touches at most `team.tasks` rows.
