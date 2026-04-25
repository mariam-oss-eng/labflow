# Changelog

All notable changes to LabFlow are documented in this file. Versions follow
[Semantic Versioning](https://semver.org/).

## [0.5.0] — 2026-04-25
### Added — Enterprise & polish
- **Role-based access control.** New `memberships` table maps API keys to a
  role (`admin` / `member` / `viewer`); destructive endpoints gated behind
  `require_role("admin")`. `GET /api/me` returns the caller's identity.
- **Encryption at rest** for `meetings.transcript` and `meetings.notes` via
  a Fernet-backed `EncryptedText` SQLAlchemy `TypeDecorator`. Set
  `LABFLOW_DATA_KEY` (urlsafe base64) to enable; `MultiFernet`-style key
  rotation is supported via comma-separated keys.
- **Server-Sent Events** at `GET /api/stream`. The dashboard now shows a
  live "live" indicator with pushed `meeting.finalized`, `task.closed`,
  `evidence.verified` events from the in-process `sse.Hub`.
- **Plugin loader.** Custom extractors and event handlers can be installed
  via standard entry-points (`labflow.plugins`) or the `LABFLOW_PLUGINS`
  env. A `PluginRegistry` is passed to each plugin's `register(api)` hook.
- **Data retention + GDPR-style export/erase.** New `retention.sweep`
  drops audit / webhook / idempotency / completed-job rows past their
  policy horizon. New admin endpoints: `GET /api/admin/export`,
  `DELETE /api/admin/erase`, `POST /api/admin/retention/sweep`.

### Changed
- OpenAPI bumped to **0.5.0**, every route tagged into one of
  `meetings / tasks / evidence / search / graph / jobs / admin / system`.

## [0.4.0] — 2026-04-25
### Added — Intelligence & integration
- **Pluggable embeddings** (`labflow.embeddings`): deterministic offline
  `HashEmbedder` default, `CallableEmbedder` hook for any
  `LABFLOW_EMBEDDING_CALLABLE=mod:fn`. Vectors persisted in a new
  `embeddings` table and refreshed eagerly on every extraction write.
- **Hybrid search.** `/api/search` now blends lexical TF (with field
  boosts), cosine over stored vectors, and a recency bias. The blend is
  controlled by `alpha` (query param or `LABFLOW_SEARCH_ALPHA`); each
  result returns a `score_components` block for explainability.
- **Decision graph.** `GET /api/graph/decisions` returns typed nodes/edges
  for the team-wide supersession graph; `.mermaid` returns a ready-to-paste
  Mermaid `flowchart` rendered server-side. The review page renders it
  inline using the Mermaid CDN script.
- **Per-team token-bucket rate limiting** middleware
  (`LABFLOW_RATE_LIMIT_PER_MINUTE` / `_BURST`) with `Retry-After` and
  `X-RateLimit-*` headers. Health/metrics paths are exempt.
- **Idempotency-Key** support on `POST/PUT/PATCH` (raw ASGI middleware,
  Stripe-style contract; cached records live in a new `idempotency_records`
  table with configurable TTL).
- **Slack notifier.** Existing webhook subscriptions whose URL points at
  `hooks.slack.com` get reshaped into Block Kit payloads automatically —
  no separate integration to wire up.

### Changed
- `extract.persist` writes embeddings inline so search remains consistent.
- Modernized web UI (Tailwind via Play CDN + HTMX, no build step) with a
  live SSE indicator, Mermaid-rendered decision graph, and an HTMX-powered
  search box that hits `/app/search` for partial-page updates.

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
