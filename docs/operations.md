# LabFlow Operations Guide

This document covers running LabFlow in production: deployment, day-2
operations, and runbooks for the most common incidents.

## Deployment topology

The minimum production deployment is three processes:

```
┌──────────┐       ┌──────────┐       ┌──────────┐
│ api (x2) │ ────► │ postgres │ ◄──── │  worker  │
└──────────┘       └──────────┘       └──────────┘
```

* **api** — `uvicorn labflow.main:app` (stateless; scale horizontally)
* **worker** — `labflow worker` (consumes the `jobs` table)
* **postgres** — single primary is fine to start; back up nightly

`docker-compose.yml` in the repo root spins this up locally. For
Kubernetes, deploy the same image with two `Deployment`s (`api`,
`worker`) and a `CronJob` for nightly tasks.

## First-boot checklist

1. **Set required env vars** (see `.env.example` for the full list):

   ```
   LABFLOW_DATABASE_URL=postgresql+psycopg://labflow:****@host:5432/labflow
   LABFLOW_AUTH_ENABLED=true
   LABFLOW_LOG_JSON=true
   LABFLOW_WEBHOOK_SIGNING_SECRET=$(openssl rand -hex 32)
   LABFLOW_GITHUB_WEBHOOK_SECRET=$(openssl rand -hex 32)
   ```

2. **Run migrations** (the Docker image does this automatically; on
   bare metal run it once before starting the API):

   ```
   alembic upgrade head
   ```

3. **Bootstrap the first team and key**:

   ```
   labflow team create acme --name "Acme Research"
   labflow keys create acme --name "ci-bot"
   # ↑ prints "lfk_…" — store it in your secret manager NOW
   ```

4. **Smoke-test**:

   ```
   curl -H "Authorization: Bearer $LFK" \
        -d '{"title":"hello","transcript":"@me will ship it"}' \
        -H 'Content-Type: application/json' \
        $LABFLOW_BASE/api/meetings
   ```

## Day-2 operations

### API key rotation

```
labflow keys create acme --name "ci-bot-2026q2"
# Update consumers to the new key.
labflow keys revoke <old-id>
```

Old keys are invalidated immediately — there is no soft-revoke window.

### Database backups

Postgres: `pg_dump --format=custom $LABFLOW_DATABASE_URL > $(date +%F).dump`
nightly. The `audit_events` table is append-only and cheap to replicate.

### Rotating the webhook signing secret

```
LABFLOW_WEBHOOK_SIGNING_SECRET=<new>
# Restart api + worker.
```

Per-subscription secrets override the default — rotate those by updating
the row in the `webhook_subscriptions` table.

## Observability

* **Health probes**:
  * `GET /healthz` — process liveness (no DB).
  * `GET /readyz` — readiness (issues `SELECT 1`).
* **Metrics**: `GET /metrics` (Prometheus text format). Key series:
  * `labflow_meetings_finalized_total{team}`
  * `labflow_tasks_closed_total{team}`
  * `labflow_evidence_verified_total{team}`
  * `labflow_jobs{status}` — gauge derived from the DB
  * `labflow_audit_events_total` — rate-of-change is your "system
    activity" signal
* **Logs**: structured JSON when `LABFLOW_LOG_JSON=true`. Every line
  carries a `request_id` field; the same id is returned to clients via
  the `X-Request-Id` header so users can include it in support tickets.

## Runbooks

### `jobs` are accumulating with `status=queued`

Symptom: `labflow_jobs{status="queued"}` rising; users report async
extraction not completing.

1. Confirm the worker is running: `docker compose ps worker` /
   `kubectl get pods -l app=labflow-worker`.
2. Inspect recent worker logs for tracebacks.
3. If the worker keeps crashing on a single job, mark it failed:

   ```sql
   UPDATE jobs SET status='failed', error='manual'
   WHERE id = <stuck-id>;
   ```

4. Restart the worker. Healthy queues drain at thousands of jobs/min.

### Webhooks are not being delivered

1. Check `webhook_deliveries` for the failing rows:

   ```sql
   SELECT id, attempts, status_code, response_body
   FROM webhook_deliveries WHERE success=false ORDER BY id DESC LIMIT 20;
   ```

2. After 5 attempts a delivery is parked. To replay, set
   `attempts = 0` and the worker (which calls `deliver_pending`) will
   retry on the next tick.
3. Verify outbound DNS / TLS from the worker container.

### A team needs to be deleted

Cascades are wired up — deleting a team row removes everything:

```sql
DELETE FROM teams WHERE slug = 'acme';
```

Audit events are deleted with the team. If you need to retain them for
compliance, dump `audit_events` first.

### Restoring from backup

1. Stop api + worker.
2. `pg_restore --clean --if-exists -d $LABFLOW_DATABASE_URL <dump>`.
3. `alembic upgrade head` (idempotent).
4. Start api + worker. Smoke-test.

## Capacity guidelines

LabFlow is I/O bound on Postgres for the workloads it targets
(transcript ingestion + low-cardinality reads). A single 4 vCPU /
16 GB Postgres comfortably supports:

* 100s of teams
* 10k meetings / day
* 100k audit events / day
* burst of 50 concurrent extractions through the worker

Beyond that, the first scaling lever is "run more workers and bump
Postgres". The `jobs` claim query is `ORDER BY id LIMIT 1` so multiple
workers contend on a single row — switch to `SELECT … FOR UPDATE SKIP
LOCKED` (Postgres-only) by overriding `_claim_one` in `jobs.py`.
