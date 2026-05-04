# Changelog

All notable changes to LabFlow are documented in this file. Versions follow
[Semantic Versioning](https://semver.org/).

## [0.11.0] — 2026-05-04
### Added — Knowledge base, smart links, GraphQL mutations, watchers, SDK
- **Wiki / knowledge base** at `/api/wiki/pages`. Slug-addressed markdown
  pages with immutable revision history (`wiki_revisions` table) and
  soft-delete. Every save writes a new revision row so the history is
  complete from page #1.
- **Smart entity links** (`labflow.links`). A regex-based parser
  materialises three patterns into the new `entity_links` table on every
  wiki / comment / transcript save:
  - `#task-123` → `ref` link to a `Task`
  - `[[Page Name]]` → `wikilink` to a `WikiPage` (auto-creates a
    placeholder page so forward links survive write order)
  - `@handle` → `mention` of an `Owner`
  Backlinks are queryable at `/api/links/backlinks?target_type=&target_id=`.
- **GraphQL mutations** (v0.7's hand-rolled engine, extended). Three
  mutations land in this release: `commentCreate`, `taskTransition`,
  `wikiPageUpsert`. They share the same scope/role enforcement as the
  REST equivalents — the GraphQL layer is a thin façade over the same
  service functions. ADR-0014.
- **Watchers + activity feed**. Per-API-key subscriptions on any entity
  (`task`, `decision`, `meeting`, `wiki`, etc.) at `/api/watchers`; the
  aggregated stream is at `/api/feed`. When the calling key has no
  watches, `/api/feed` falls back to the team-wide audit log so admins
  can see everything without bookkeeping.
- **TypeScript client SDK** (`sdk/typescript/`). Hand-curated, dependency-free
  client (~150 lines) covering the 80% common path: meetings, tasks,
  decisions, wiki, search, and dashboards. Built with `tsc`, no runtime
  dependencies, suitable for both Node and browser.
- **GitHub Pages** — the docs site at
  https://mariam-oss-eng.github.io/labflow/ is now built and published
  from the `pages.yml` workflow on every push to `main`.

### Changed
- The `parse()` function in `labflow.graphql_api` now records the
  operation kind (`query` vs `mutation`) on the parser. Existing
  callers using the public function are unaffected — the back-compat
  signature is preserved; new callers can use `parse_with_op` to access
  the operation kind.

### Migration
- `f4c5d77ef2c3` — adds `wiki_pages`, `wiki_revisions`, `entity_links`,
  and `watchers`. (Same migration also covers v0.10.) Backward
  compatible: no existing data is mutated.

## [0.10.0] — 2026-05-04
### Added — Tamper-evident audit, signed backups, automation, forecasting, dashboards
- **Tamper-evident audit chain** in `audit_events`. Every row carries
  `prev_hash` (= previous row's `entry_hash` for the same team) and
  `entry_hash` (= sha256 over the canonical row payload). The new
  `/api/audit/verify` endpoint walks the chain and reports the first
  break. Pre-v0.10 NULL-hash rows are treated as legacy genesis pivots
  so upgraded databases keep verification meaningful. ADR-0011.
- **Signed full-team backup & restore** at `/api/admin/backup` and
  `/api/admin/restore/{preview,apply}`. The envelope is HMAC-SHA256
  signed with an operator-supplied secret; restore requires a
  *different* slug so a recovery never silently overwrites live data.
  Dependency-free (no compression / encryption layer baked in — pipe
  through `age` / `gpg` if you need confidentiality at rest).
- **Automation rules engine** at `/api/automation/rules`. Declarative
  when/then JSON rules with three built-in action kinds (`tag`,
  `notify`, `webhook`). The dispatcher is invoked from `audit.record`
  so any audited action can trigger automation; rule failures are
  logged but never block the underlying state change. ADR-0012.
- **Burndown forecasting** at `/api/forecast/sprint/{slug}` (least-squares
  ETA + ±1σ confidence band) and `/api/forecast/task/{id}` (per-task
  ETA from owner's median historical cycle time, with a team-wide
  fallback). Pure Python — no scipy / numpy dependency.
- **Customisable dashboards** at `/api/dashboards`. Per-API-key widget
  layouts; the server ships a five-widget catalogue (`open_tasks`,
  `recent_decisions`, `sla_breaches`, `sprint_burndown`,
  `automation_status`) that the `/api/dashboards/{slug}/data` endpoint
  renders into a single response.

### Migration
- `f4c5d77ef2c3` — adds `prev_hash` + `entry_hash` columns to
  `audit_events` (both nullable, so legacy rows remain valid),
  `automation_rules`, and `dashboards`. No existing data is rewritten.

## [0.9.0] — 2026-04-30
### Added — AI, Plugins, Vector v2, Multi-region, Time-travel, PWA & i18n
- **AI Copilot** at `/api/copilot`. Multi-step *tool-using* agent over the
  team's data with seven built-in tools (`search`, `get_task`,
  `list_open_tasks`, `list_decisions`, `summarize_meeting`, `propose_task`,
  `analytics`). Ships with a deterministic offline planner so the feature
  works without an LLM; production deployments can plug in OpenAI / Claude
  / a local model via `LABFLOW_COPILOT_LLM_CALLABLE`. Every turn — user
  message, tool calls, assistant reply — is persisted in
  `copilot_sessions.transcript_json` and the audit log, so any interaction
  is replayable. `propose_task` is intentionally side-effect-free (drafts
  only) to keep the agent safe.
- **Plugin marketplace** at `/api/plugins`. Operator-facing install /
  enable / disable / uninstall lifecycle for *signed manifests*: each
  manifest is canonical-JSON-hashed (SHA-256) and the operator passes the
  expected hash on install. Manifests declare `permissions` (subset of
  v0.8 scopes) and `hooks` (typed event names). The catalogue is separate
  from v0.5's runtime extension loader; ADR-0008 sketches the v1.0
  sandbox plan.
- **Vector index v2**: pure-Python HNSW-style on-disk shards persisted at
  `LABFLOW_VECTOR_INDEX_DIR/<team_id>/`, with snapshot rotation via the
  new `vector_index_shards` table. Brute-force fallback when no shard is
  built (and for new teams in tests). Optional cross-encoder re-rank pass
  with a built-in phrase-boost heuristic; `LABFLOW_RERANKER_CALLABLE`
  hook for plugging in a real cross-encoder. ADR-0007.
- **Read-replica routing** via `labflow.replica.read_session()` /
  `write_session()` context managers. Round-robins reads across
  `LABFLOW_READ_REPLICA_URLS` (comma-separated SQLAlchemy URLs); falls
  back to the primary when no replicas are configured. New
  `/readyz/replicas` endpoint reports per-replica health. ADR-0008.
- **Time-travel queries** at `/api/timetravel/{tasks,meetings}/{id}?as_of=ISO`.
  Reconstructs the historical state of a task or meeting by walking the
  `audit_events` log in reverse. Validates the `as_of` window against
  `LABFLOW_TIMETRAVEL_MAX_DAYS`; returns `incomplete: true` when an
  audit action lacks rewind metadata.
- **Installable PWA**: `manifest.webmanifest`, service worker
  (`/static/sw.js`) with cache-first static + network-first JSON +
  offline shell fallback (`/static/offline.html`). Installable via
  add-to-home-screen on iOS, Android, and desktop Chromium.
- **i18n (en · es · fr)**: dict-based message catalogues with RFC 7231
  `Accept-Language` negotiation, served at `/api/i18n/messages`. Adding a
  locale is a single PR to `labflow/i18n.py`.

### Models / migration
- New tables: `plugins`, `copilot_sessions`, `vector_index_shards`.
- Migration: `e3b4c66de1b2_v0_9_plugins_copilot_vector.py`.

### Tests
- 200 total, all green (was 175 in 0.8).

---

## [0.8.0] — 2026-04-29
### Added — Workflows, Permissions, Sprints, Sharing
- **Configurable workflow / state-machine engine** for tasks. Per-team
  workflows are JSON documents (`states`, `initial`, `terminal`,
  `transitions`); a sensible default is auto-created. Endpoints
  `POST /api/workflows`, `POST /api/tasks/{id}/transition`, and
  `POST /api/admin/sla/sweep`. Transitions are audit-logged, fire SSE
  events, and may set an SLA breach timestamp; the sweep endpoint emits
  `task.sla.breach` events for late tasks.
- **Sprints / iterations** at `/api/sprints` with a deterministic burndown
  endpoint that reconstructs daily remaining-task counts from the audit
  log. Sprints are slug-addressed and may be open or closed.
- **Task DAG + critical path** at `/api/tasks/{id}/depends_on` and
  `/api/tasks/critical-path`. Cycle-checked on insert (Kahn topo sort);
  longest-weighted-path DP picks the critical chain using a confidence-
  weighted effort heuristic.
- **Resource-level ACLs** at `/api/acl`. Two-tier model on top of the
  existing RBAC roles: when no ACL exists for a row the role check
  alone applies (back-compat); when any ACL exists, only listed keys
  may touch it. `acl.assert_allowed` is wired into task transitions.
- **Signed share links** at `/api/share-links` and `/api/share/{token}`.
  Tokens are stored hashed (SHA-256), optional passcode is constant-
  time compared, TTL enforced, and links are explicitly revocable.
- **API-key scopes**: `read / write / admin / webhook:emit /
  plugin:install`. Legacy keys (NULL scopes) keep all permissions for
  back-compat; new keys must declare scopes.
- **CSV exports** at `/api/exports/{tasks,decisions}.csv`. RFC 4180
  compliant (CRLF + minimal quoting) with a UTF-8 BOM so Excel auto-
  detects the encoding correctly.
- **Slack-compatible notifier** at `/api/notify/slack/digest` posting a
  Slack `blocks` payload of the weekly digest. Stdlib `urllib`, optional
  `X-LabFlow-Signature` HMAC, no extra dependency.

### Models / migration
- New tables: `workflows`, `sprints`, `resource_acls`, `share_links`.
- New columns on `tasks`: `workflow_id`, `sprint_id`, `state`,
  `sla_breach_at`. New column on `api_keys`: `scopes`.
- Migration: `d2a3b55ce0a1_v0_8_workflows_sprints_acls.py`.

### Tests
- 175 total, all green (was 154 in 0.7).

---

## [0.7.0] — 2026-04-26
### Added — Realtime, GraphQL & Observability
- **WebSocket** at `/ws` with bidirectional protocol: `subscribe` /
  `unsubscribe` (supports `task.*`-style prefix wildcards), `ping`/`pong`,
  and a 20s server heartbeat. Same in-process hub feeds both SSE and WS,
  so all clients see the same event stream.
- **Read-only GraphQL** at `POST /graphql`. Hand-rolled tokenizer +
  recursive-descent parser, resolvers for `team / tasks / decisions /
  meetings / comments / analytics`, and a `__schema` introspection field.
  Zero new runtime dependencies.
- **OpenTelemetry** auto-instrumentation: `LABFLOW_OTEL_ENABLED=true`
  sets up TracerProvider + MeterProvider, instruments FastAPI and
  SQLAlchemy when the SDK is importable. The `otel.span()` context manager
  is a no-op when OTEL is absent so call sites don't need feature flags.
- **PostgreSQL FTS backend** for `/api/search`. Auto-detected via the
  SQLAlchemy dialect; uses `to_tsvector @@ to_tsquery` with `ts_rank`
  ordering when on Postgres, falls back to the existing `ILIKE` path on
  SQLite. No schema change required (operators add a GIN index in prod).
- **Distributed worker leader-lock**: a new `worker_locks` table holds
  a leased `(name, owner, expires_at)` row so multiple API replicas can
  run safely behind a load balancer with the worker active on at most
  one. Heartbeats refresh the lease; a crashed leader is stolen after
  TTL.
- **Official Python SDK** in the `labflow_client` package. Sync +
  async clients, uses `httpx` when installed and falls back to stdlib
  `urllib`. Typed methods for meetings, tasks, evidence, search,
  comments/reactions, analytics, GraphQL, and SDK-friendly
  `Idempotency-Key` plumbing.
- **Helm chart** under `deploy/helm/labflow/` for HA deployment on
  Kubernetes (multi-replica API + Postgres + leader-elected worker).
- **OpenAPI bumped to 0.7.0**, every new route tagged
  `collab / saved-searches / analytics / calendar / graphql`.

### Changed
- `__version__ = "0.7.0"`. Healthz reports the same.

## [0.6.0] — 2026-04-26
### Added — Collaboration & Insights
- **Threaded comments** on decisions and tasks via `GET/POST /api/comments`
  (and soft-delete via `DELETE /api/comments/{id}`). Replies form a tree
  through `parent_id`; the API returns flat rows and a nested view.
- **Emoji reactions** via `POST /api/reactions` with toggle semantics —
  posting the same emoji twice removes it. Counts are returned alongside
  the toggle response so the UI doesn't need a follow-up GET.
- **Saved searches** (`GET/POST/DELETE /api/saved-searches`,
  `GET /api/saved-searches/{slug}/run`). Auto-slugified names,
  alpha+filters overrides, and pinning for the dashboard sidebar.
- **Analytics endpoint** at `GET /api/analytics?days=N` returning
  meetings, decisions, tasks-opened/-closed, cycle-time p50/p90,
  completion rate, blocker rate, top owners, and an 8-week
  opened/closed trend (the dashboard renders the latter as a sparkline).
- **iCalendar feed** at `GET /api/calendar.ics`. Hand-rolled RFC 5545
  output (escapes commas/semicolons/backslashes/CRLF, line folding to
  75 octets) so any calendar client can subscribe to upcoming task due
  dates.
- **AI summary** at `GET /api/meetings/{id}/summary?max_sentences=N`.
  Default backend is **TextRank-lite** (sentence similarity graph +
  power-iteration PageRank) — pure Python, deterministic, offline. Set
  `LABFLOW_SUMMARY_CALLABLE=pkg.mod:fn` to plug in any LLM.
- **HTML email digest** at `GET /api/digest/weekly.html` using inline
  styles (the only thing that survives Gmail's `<style>` stripping) and
  table-free markup that renders well on mobile.
- **Per-API-key notification preferences** via `GET/PUT
  /api/me/notifications`. Cadence (`off|daily|weekly`), email, and a
  mute list of event types.
- **Audit + SSE coverage** for every collaboration write so existing
  dashboards see comment activity in real time without a code change.

### Changed
- `__version__ = "0.6.0"`. New OpenAPI tags `collab / saved-searches /
  analytics / calendar`.

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
