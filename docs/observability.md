# Observability

LabFlow is instrumented for production from day one: structured JSON
logging with request IDs (v0.4), Prometheus metrics at `/metrics`
(v0.3), and **OpenTelemetry traces + metrics** (v0.7) when the SDK is
installed and `LABFLOW_OTEL_ENABLED=true`.

## Enable OpenTelemetry

```bash
pip install \
  opentelemetry-sdk \
  opentelemetry-exporter-otlp-proto-http \
  opentelemetry-instrumentation-fastapi \
  opentelemetry-instrumentation-sqlalchemy

export LABFLOW_OTEL_ENABLED=true
export OTEL_EXPORTER_OTLP_ENDPOINT=https://my-collector:4318
export OTEL_RESOURCE_ATTRIBUTES=deployment.environment=prod
```

Without the SDK installed, the env var is harmless — the `otel.span()`
helper compiles to a no-op context manager so call sites don't need
feature flags.

## What we instrument automatically

* **HTTP requests** via `FastAPIInstrumentor` (route, status, duration).
* **SQLAlchemy queries** via `SQLAlchemyInstrumentor` (statement,
  parameters redacted, dialect).
* **Hot inner spans** at named call sites:
    * `labflow.summarize` — `/api/meetings/{id}/summary`
    * `labflow.graphql`  — `/graphql`
    * `labflow.search`   — hybrid candidate fetch + re-rank
    * `labflow.webhook.deliver` — outbound HTTP delivery
    * `labflow.job.run`  — background job execution

Each carries useful attributes (`team_id`, `meeting_id`, etc.) so
trace search by attribute works out of the box.

## Metrics

`/metrics` exposes Prometheus counters and histograms:

| Metric | Type | Labels |
| --- | --- | --- |
| `labflow_http_requests_total` | counter | `method`, `path`, `status` |
| `labflow_http_request_duration_seconds` | histogram | same |
| `labflow_jobs_processed_total` | counter | `name`, `status` |
| `labflow_webhook_deliveries_total` | counter | `event`, `status` |
| `labflow_search_requests_total` | counter | `backend` (`pg_fts` / `lexical`) |

Wire this directly into `prometheus-operator` `ServiceMonitor` rules.

## Logging

Every request emits a single structured log line with `request_id`,
`team_id`, `actor`, `path`, `status`, and `duration_ms`. Log format is
JSON via `python-json-logger`; pipe `stdout` to your aggregator.
