# Architecture decision records

ADRs document the *why* behind significant technical choices in
LabFlow. Each is short (one page or less), dated, and immutable —
when a decision is reversed, we add a new ADR that supersedes it.

| # | Status | Title |
| :-- | :-- | :-- |
| [0001](0001-hybrid-search.md) | Accepted | Hybrid search ranking |
| [0002](0002-idempotency-middleware.md) | Accepted | Idempotency as raw ASGI middleware |
| [0003](0003-encryption-at-rest.md) | Accepted | Encryption at rest with Fernet |
| [0004](0004-collaboration-model.md) | Accepted | Collaboration as generic comments / reactions |
| [0005](0005-websocket-vs-sse.md) | Accepted | Keep both SSE and WebSocket; share a single hub |
| [0006](0006-hand-rolled-graphql.md) | Accepted | Hand-rolled GraphQL instead of a framework |
