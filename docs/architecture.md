# Architecture

LabFlow is a single Python package (`labflow/`) with five concerns,
each isolated in its own module:

| Concern | Module(s) | Notes |
| --- | --- | --- |
| HTTP surface | `main.py` | FastAPI app factory, route registration |
| Persistence | `db.py`, `models.py`, `migrations/` | SQLAlchemy 2 + Alembic |
| Domain logic | `extraction.py`, `services.py`, `verification.py` | Pure functions; no FastAPI imports |
| Search & graph | `embeddings.py`, `embedding_store.py`, `search.py`, `graph.py` | Hybrid lexical + semantic + recency |
| Cross-cutting | `auth.py`, `ratelimit.py`, `idempotency.py`, `crypto.py`, `audit.py`, `webhooks.py`, `sse.py`, `plugins.py`, `retention.py` | Each is independent |

## Request flow

```mermaid
flowchart TD
    R[Request] --> RL[RateLimitMiddleware]
    RL --> ID[IdempotencyMiddleware]
    ID --> CTX[Request-id + access-log middleware]
    CTX --> AUTH[require_team / require_role]
    AUTH --> RT[Route handler]
    RT --> SVC[Service / extraction]
    SVC --> DB[(SQL)]
    SVC --> EMB[(embeddings)]
    RT --> AU[audit.record]
    RT --> WH[webhooks.emit]
    RT --> SSE[sse.hub.publish]
    WH --> JQ[Job queue]
    JQ --> WK[Worker]
    WK --> EX[External webhook / Slack]
```

## Key design decisions

See the ADR index for full motivation:

* **[ADR-0001 · Hybrid search ranking](adr/0001-hybrid-search.md)** —
  why we blend lex/semantic/recency and expose `score_components`.
* **[ADR-0002 · Idempotency as raw ASGI](adr/0002-idempotency-middleware.md)** —
  why `BaseHTTPMiddleware` was the wrong primitive.
* **[ADR-0003 · Encryption at rest with Fernet](adr/0003-encryption-at-rest.md)** —
  why we picked Fernet + `TypeDecorator` and what's *not* encrypted.

## Data model (high level)

```mermaid
erDiagram
    TEAMS ||--o{ MEETINGS : has
    MEETINGS ||--o{ DECISIONS : extracts
    MEETINGS ||--o{ TASKS : extracts
    MEETINGS ||--o{ EXPERIMENTS : extracts
    DECISIONS }o--|| DECISIONS : superseded_by
    TASKS ||--o{ EVIDENCE : verified_by
    TEAMS ||--o{ API_KEYS : owns
    API_KEYS ||--o| MEMBERSHIPS : "v0.5 role"
    TEAMS ||--o{ AUDIT_EVENTS : records
    TEAMS ||--o{ EMBEDDINGS : "v0.4 vectors"
    TEAMS ||--o{ IDEMPOTENCY_RECORDS : "v0.4"
```
