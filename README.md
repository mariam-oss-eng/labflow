<div align="center">

# 🧪 LabFlow

**The meeting → execution operating system for research and technical teams.**

[![Tests](https://img.shields.io/badge/tests-154%20passing-brightgreen)](#testing)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-informational)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.7.0-6366f1)](CHANGELOG.md)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688)](https://fastapi.tiangolo.com)
[![GraphQL](https://img.shields.io/badge/GraphQL-read--only-e10098)](docs/graphql.md)
[![WebSocket](https://img.shields.io/badge/WebSocket-bidirectional-2563eb)](docs/realtime.md)
[![SDK](https://img.shields.io/badge/Python%20SDK-labflow__client-yellow)](labflow_client/)
[![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-optional-425cc7)](docs/observability.md)
[![Docs](https://img.shields.io/badge/docs-mkdocs--material-9c27b0)](docs/)

LabFlow turns transcripts, calls, and planning docs into a **typed, queryable
graph** of decisions, action items, experiments, owners, deadlines, and
evidence of completion — with an audit log, webhooks, live search,
realtime updates, GraphQL, an iCalendar feed, and an official Python SDK.
Built for ML research groups, AI/biotech labs, and prototype-heavy startup
teams. Not another notes app.

[Quickstart](#quickstart) · [Architecture](#architecture) · [Feature matrix](#feature-matrix) · [Documentation](docs/) · [Changelog](CHANGELOG.md) · [Python SDK](labflow_client/)

</div>

---

## Why LabFlow

Most meeting assistants stop at a Markdown summary. **LabFlow ships an
execution structure plus a verification, collaboration, and analytics layer
on top:**

* **Typed extraction** — decisions, tasks, experiments, assumptions, and
  blockers are separate first-class entities, each with confidence and a
  source span back to the transcript.
* **Decision graph** — the same statement across two meetings creates a
  supersession edge automatically; the team gets a long-lived
  decision-history they can query and render as a Mermaid diagram.
* **Evidence-driven completion** — tasks are closed by attaching commits,
  PRs, datasets, or artifacts. A built-in verifier scores the match;
  inbound GitHub webhooks attach evidence on their own.
* **Hybrid search** — keyword TF + cosine similarity over stored
  embeddings + recency, with explainable per-result `score_components`.
  Auto-upgrades to PostgreSQL `to_tsvector` when on Postgres.
* **Live everything** — Server-Sent Events *and* WebSockets stream
  finalize / close / verify / comment / react events to the dashboard.
* **Collaboration** — threaded comments and emoji reactions on every
  decision and task; saved searches; HTML email digests; per-user
  notification preferences.
* **Insights, not dashboards** — a single `/api/analytics` endpoint
  returns cycle-time p50/p90, throughput, completion rate, blocker rate,
  top owners, and an 8-week trend.
* **Power-user surfaces** — read-only **GraphQL** at `/graphql`,
  iCalendar feed at `/api/calendar.ics`, and a typed **Python SDK**.
* **Production-grade** — RBAC, encryption at rest, OpenTelemetry traces,
  Helm chart, distributed worker leader-lock for HA replicas.

## Architecture

```mermaid
flowchart LR
    subgraph Client
      U[Browser / CLI / API]
    end
    subgraph LabFlow API
      MW1[RateLimit] --> MW2[Idempotency] --> R[Routes]
      R --> EX[Extraction]
      R --> SR[Hybrid search]
      R --> GR[Decision graph]
      R --> RBAC[RBAC]
    end
    subgraph Persistence
      DB[(Postgres / SQLite)]
      EMB[(embeddings)]
      AUD[(audit_events)]
    end
    subgraph Async
      JQ[Job queue] --> WK[Worker]
      WH[Webhook deliverer] --> SLK[Slack]
      WH --> GH[GitHub]
    end
    U --> MW1
    R --> DB
    EX --> EMB
    R --> JQ
    R --> WH
    R -. SSE .-> U
```

Single-process by default (FastAPI + SQLAlchemy + Alembic + an
in-process job worker). Scale horizontally by pointing at Postgres and
running multiple replicas behind any HTTP load balancer.

## Quickstart

### Local (SQLite)

```bash
git clone https://github.com/mariam-oss-eng/labflow.git
cd labflow
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head
labflow serve  # → http://localhost:8000/app
```

Open the dashboard, paste a transcript, hit **Extract →**.

### Docker

```bash
docker compose up --build
```

Boots the API + a worker against a Postgres container.

### One-shot CLI

```bash
echo "@alice will retrain the model. We decided to adopt SentencePiece." \
  | labflow ingest --title "Standup"
labflow digest --weekly
```

## Feature matrix

| Capability | v0.1 | v0.2 | v0.3 | v0.4 | v0.5 | **v0.6** | **v0.7** |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| Typed extraction (decisions / tasks / experiments) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Multi-tenancy + API keys | — | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Postgres + Alembic migrations | — | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Background job queue + worker | — | — | ✅ | ✅ | ✅ | ✅ | ✅ |
| Append-only audit log | — | — | ✅ | ✅ | ✅ | ✅ | ✅ |
| Outbound webhooks (HMAC-signed) | — | — | ✅ | ✅ | ✅ | ✅ | ✅ |
| Inbound GitHub evidence webhook | — | — | ✅ | ✅ | ✅ | ✅ | ✅ |
| Prometheus metrics | — | — | ✅ | ✅ | ✅ | ✅ | ✅ |
| Pluggable embeddings | — | — | — | ✅ | ✅ | ✅ | ✅ |
| Hybrid keyword + semantic search | — | — | — | ✅ | ✅ | ✅ | ✅ |
| Decision graph + Mermaid renderer | — | — | — | ✅ | ✅ | ✅ | ✅ |
| Per-team rate limiting | — | — | — | ✅ | ✅ | ✅ | ✅ |
| Idempotency-Key on writes | — | — | — | ✅ | ✅ | ✅ | ✅ |
| Slack notifier | — | — | — | ✅ | ✅ | ✅ | ✅ |
| Role-based access control | — | — | — | — | ✅ | ✅ | ✅ |
| Encryption at rest (Fernet) | — | — | — | — | ✅ | ✅ | ✅ |
| Server-Sent Events live stream | — | — | — | — | ✅ | ✅ | ✅ |
| Plugin loader (entry-point + dotted) | — | — | — | — | ✅ | ✅ | ✅ |
| GDPR export / erase + retention sweep | — | — | — | — | ✅ | ✅ | ✅ |
| **Threaded comments + emoji reactions** | — | — | — | — | — | ✅ | ✅ |
| **Saved searches** | — | — | — | — | — | ✅ | ✅ |
| **Analytics endpoint (cycle time, trend)** | — | — | — | — | — | ✅ | ✅ |
| **iCalendar (.ics) feed** | — | — | — | — | — | ✅ | ✅ |
| **AI summary (TextRank + LLM hook)** | — | — | — | — | — | ✅ | ✅ |
| **HTML email digest + notification prefs** | — | — | — | — | — | ✅ | ✅ |
| **WebSocket bidirectional channel** | — | — | — | — | — | — | ✅ |
| **Read-only GraphQL endpoint** | — | — | — | — | — | — | ✅ |
| **OpenTelemetry traces + metrics** | — | — | — | — | — | — | ✅ |
| **PostgreSQL full-text search backend** | — | — | — | — | — | — | ✅ |
| **Distributed worker leader-lock** | — | — | — | — | — | — | ✅ |
| **Official Python SDK (`labflow_client`)** | — | — | — | — | — | — | ✅ |
| **Helm chart for Kubernetes** | — | — | — | — | — | — | ✅ |

## Configuration

Every setting reads from environment variables (12-factor):

| Variable | Default | Notes |
| --- | --- | --- |
| `LABFLOW_DATABASE_URL` | `sqlite:///./labflow.db` | Postgres URL recommended in prod |
| `LABFLOW_AUTH_ENABLED` | `false` | Multi-tenant API-key auth |
| `LABFLOW_RATE_LIMIT_PER_MINUTE` | `600` | `0` disables |
| `LABFLOW_RATE_LIMIT_BURST` | `60` | Token bucket capacity |
| `LABFLOW_IDEMPOTENCY_TTL_SECONDS` | `86400` | Cache window for `Idempotency-Key` |
| `LABFLOW_EMBEDDING_DIM` | `256` | Hash embedder dimensionality |
| `LABFLOW_EMBEDDING_CALLABLE` | _(unset)_ | `pkg.mod:fn` for real embeddings |
| `LABFLOW_SEARCH_ALPHA` | `0.5` | Hybrid blend (0=lex, 1=semantic) |
| `LABFLOW_DATA_KEY` | _(unset)_ | Fernet key — enables encryption at rest |
| `LABFLOW_PLUGINS` | _(unset)_ | Comma-separated `pkg.mod:obj` plugin specs |
| `LABFLOW_WEBHOOK_SIGNING_SECRET` | _(generated)_ | HMAC-SHA256 secret for outbound hooks |
| `LABFLOW_SUMMARY_CALLABLE` | _(unset)_ | `pkg.mod:fn` for an LLM-backed summarizer |
| `LABFLOW_OTEL_ENABLED` | `false` | Auto-instrument with OpenTelemetry when set |

## API highlights

| Method | Path | Description |
| :-- | --- | --- |
| `POST` | `/api/meetings` | Create + auto-extract a meeting (supports `Idempotency-Key`) |
| `POST` | `/api/meetings/{id}/finalize` | Lock a meeting; emits `meeting.finalized` |
| `GET`  | `/api/meetings/{id}/summary` | TextRank or LLM-backed summary _(v0.6)_ |
| `GET`  | `/api/search?q=&alpha=` | Hybrid search w/ explainable score components |
| `GET`  | `/api/graph/decisions` | Decision graph nodes/edges as JSON |
| `GET`  | `/api/analytics?days=30` | Cycle time, throughput, weekly trend _(v0.6)_ |
| `GET`  | `/api/calendar.ics` | iCalendar feed of upcoming due dates _(v0.6)_ |
| `GET`/`POST` | `/api/comments` | Threaded comments _(v0.6)_ |
| `POST` | `/api/reactions` | Toggle emoji reactions _(v0.6)_ |
| `GET`/`POST`/`DELETE` | `/api/saved-searches` | Pinnable named queries _(v0.6)_ |
| `GET`  | `/api/digest/weekly.html` | HTML email digest _(v0.6)_ |
| `GET`  | `/api/stream` | Server-Sent Events stream for the team |
| `WS`   | `/ws` | **Bidirectional** realtime channel _(v0.7)_ |
| `POST` | `/graphql` | Read-only GraphQL endpoint _(v0.7)_ |
| `GET`  | `/api/me` | Caller's identity, role, and posture |
| `GET`  | `/api/admin/export` | GDPR Article 15 export of every team row |
| `DELETE` | `/api/admin/erase` | GDPR Article 17 hard-delete (admin only) |
| `GET`  | `/healthz`, `/readyz`, `/metrics` | Liveness, readiness, Prometheus |

Full schema: visit `/docs` (Swagger UI), `/openapi.json`, or `/graphql/schema`.

## Python SDK

```python
from labflow_client import LabFlow

lf = LabFlow("https://labflow.example.com", api_key="lfk_...")
m = lf.create_meeting(title="Standup", transcript=open("notes.md").read())
print(lf.summarize_meeting(m["id"])["summary"])
for t in lf.list_tasks(status="open")["items"]:
    print(t["title"], "→", t["due_date"])
print(lf.analytics(days=14)["cycle_time_p50_days"])
```

The SDK ships sync + async clients, uses `httpx` when installed and falls
back to the stdlib `urllib` so it works in any environment.

## GraphQL

```graphql
query {
  team { slug }
  tasks(status: "open", limit: 10) { id title owner due_date }
  analytics(days: 30) { tasks_closed cycle_time_p50_days }
}
```

`POST /graphql` with `{"query": "..."}`. Selection sets, args, and
`__schema` introspection are all supported. See [docs/graphql.md](docs/graphql.md).

## Realtime (WebSocket)

```javascript
const ws = new WebSocket("wss://labflow.example.com/ws?token=lfk_...");
ws.onmessage = (m) => console.log(JSON.parse(m.data));
ws.send(JSON.stringify({type: "subscribe", events: ["task.closed"]}));
```

See [docs/realtime.md](docs/realtime.md) for filter syntax and the
event catalog.

## Deployment

* `docker compose up --build` — full stack on Docker for local Postgres.
* `helm install labflow deploy/helm/labflow` — production HA on Kubernetes
  (multiple API replicas, leader-elected worker, OTEL collector hooks).
* Bare metal: `pip install labflow && uvicorn labflow.main:create_app
  --factory --workers 4 --proxy-headers`.

## Testing

```bash
pytest          # 154 tests, ~11s on a laptop
pytest -k v06   # subset
```

Coverage spans model logic, API contract, RBAC, encryption round-trip,
idempotency replay, rate-limit headers, hybrid search ranking, decision
graph rendering, plugin loading, retention sweep, Slack payload,
collaboration (comments/reactions/saved-searches), iCalendar feed,
TextRank summary, GraphQL query/introspection/error reporting, WebSocket
hello/subscribe/ping, distributed worker lock acquire/heartbeat/steal,
and the Python SDK against an in-memory app.

## Documentation

The full documentation site is built with [MkDocs Material](docs/mkdocs.yml)
and published to GitHub Pages on every push to `main`.

* [Operations runbook](docs/operations.md)
* [Onboarding guide](docs/onboarding.md)
* [Product spec](docs/product_spec.md)
* [Architecture decision records](docs/adr/)
* [Contributing](CONTRIBUTING.md)

## License

MIT — see [LICENSE](LICENSE).
