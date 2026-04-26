# Deployment

LabFlow is a single Python package + a single ASGI app. Three supported
deployment shapes:

## 1. Local — SQLite (development)

```bash
pip install -e ".[dev]"
alembic upgrade head
labflow serve            # → http://localhost:8000/app
```

The default `labflow.db` SQLite file is fine for prototyping and small
single-team installs.

## 2. Docker Compose — Postgres + worker (small teams)

```bash
docker compose up --build
```

This boots the API behind Postgres and runs the in-process worker.
Suitable for teams up to a few thousand meetings/month on a single VM.

## 3. Kubernetes — Helm chart (production HA)

```bash
helm install labflow deploy/helm/labflow/ \
  --set image.repository=ghcr.io/yourorg/labflow \
  --set image.tag=0.7.0 \
  --set postgres.url=postgres://labflow:...@pg/labflow \
  --set replicaCount=3
```

What you get:

* **Multi-replica API** behind a Service + Ingress. Stateless replicas
  share the database; SSE/WebSocket connections stick via session
  affinity.
* **Distributed worker leader-lock** (`labflow.worker_lock`) so exactly
  one replica runs the background queue at a time. Crashed leader is
  auto-stolen after the lease TTL — no operator intervention.
* **PostgreSQL FTS** auto-detected, so search is fast at scale.
* Resource requests / limits, liveness + readiness probes, and a
  PodDisruptionBudget set by default.
* Optional: `--set otel.enabled=true` wires up the OTLP exporter to
  whatever collector you've set in `OTEL_EXPORTER_OTLP_ENDPOINT`.

## Environment variables

The Helm chart's `values.yaml` exposes all `LABFLOW_*` env vars under
`env:` so you can override anything from the [README](../README.md#configuration)
table without editing manifests.

## Migrations

```bash
alembic upgrade head      # idempotent; safe to run on every boot
```

The chart's `initContainer` does this automatically.

## Backups

We do **not** ship a backup operator — Postgres has its own. The two
columns we encrypt at rest (`meetings.transcript`, `meetings.notes`)
remain encrypted in `pg_dump` output, so backups are safe to ship to
S3 directly. Make sure `LABFLOW_DATA_KEY` is rotated through your
secrets manager, not committed.
