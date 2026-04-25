# Changelog

All notable changes to LabFlow are documented in this file. Versions follow
[Semantic Versioning](https://semver.org/).

## [0.3.0] — 2026-04-25
### Added
- **Background job queue** (in-process worker, `Job` table) and asynchronous
  extraction endpoint that returns `202 Accepted` + a job id. Synchronous
  extraction remains available for backward compatibility.
- **Audit log** (`AuditEvent` table) appended on every meaningful state
  change (meeting created/finalized, task closed, evidence verified, …).
- **Outbound webhooks** with HMAC-SHA256 signing, retry/backoff, and a
  delivery log. Events: `meeting.finalized`, `task.closed`,
  `evidence.verified`, `digest.published`.
- **Inbound GitHub webhook** that converts new commits/PRs into evidence
  rows and re-runs verification.
- **Search endpoint** (`/api/search`) over decisions and tasks with simple
  ranking suitable for SQLite and Postgres.
- **Prometheus metrics** at `/metrics` (no extra dependencies — built on
  the stdlib `prometheus_client` package or a hand-rolled exporter).
- **CLI** (`labflow` console script): `migrate`, `serve`, `worker`,
  `ingest`, `digest`, `team create`, `keys create`, `keys revoke`.
- **Operations docs** (`docs/operations.md`) describing deployment, key
  rotation, backups, and runbooks.

### Changed
- Default Docker entrypoint runs `alembic upgrade head` before serving.
- The worker process is shipped as a separate `docker compose` service.

## [0.2.0] — 2026-04-25
### Added
- **Multi-tenancy**: every row carries a `team_id`. New `Team` and `ApiKey`
  models, with API-key auth via `Authorization: Bearer …` or
  `X-LabFlow-Key`. Single-team mode preserved when `LABFLOW_AUTH_ENABLED=false`.
- **Typed configuration** via `pydantic-settings`. Configuration errors
  now fail fast at startup with a clear message.
- **Structured logging** with a request-id middleware and either JSON or
  human-readable formatter (`LABFLOW_LOG_JSON`).
- **Consistent error envelope** (`{"error": {"code", "message", "details",
  "request_id"}}`) for every error response.
- **Pagination + filtering** on `GET /api/meetings` and `GET /api/tasks`.
- **Pluggable extractor backends**: `RulesBackend` (default, offline) and
  `LLMBackend` that delegates to a configurable callable and falls back
  to rules on any error.
- **Alembic migrations**, replacing ad-hoc `create_all` for production
  deployments.
- **Dockerfile** (multi-stage, non-root) and `docker-compose.yml`
  (Postgres + API + worker).
- **GitHub Actions** CI matrix on Python 3.10/3.11/3.12 that runs
  migrations + the test suite.
- `.env.example` documenting every supported environment variable.

### Changed
- Replaced every `datetime.utcnow()` call site with the timezone-aware
  `now_utc()` helper. Persisted timestamps remain naive UTC for SQLite
  compatibility but Python-side code is tz-aware throughout.
- `POST /api/meetings`, `POST /api/meetings/upload`, and
  `POST /api/evidence` now return `201 Created`.
- `POST /api/meetings/{id}/extract` on a finalized meeting returns
  `409 Conflict` (was `400`).
- `PATCH /api/tasks/{id}` accepts a strict, validated payload — extra
  fields return `422`.

### Removed
- The undocumented `LABFLOW_LLM_PROVIDER` knob is replaced by
  `LABFLOW_EXTRACTION_BACKEND` + `LABFLOW_LLM_CALLABLE`.

## [0.1.0] — 2026-04-25
### Added
- Initial MVP: meeting upload, deterministic rule-based extraction
  (decisions, tasks, experiments, assumptions, blockers), evidence-driven
  verification, weekly digest, JSON/Markdown export, and a minimal HTML
  dashboard.
