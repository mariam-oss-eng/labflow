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
| [0007](0007-vector-index-v2.md) | Accepted | Pure-Python HNSW vector index (v0.9) |
| [0008](0008-replica-routing.md) | Accepted | Read-replica routing (v0.9) |
| [0009](0009-copilot-tool-agent.md) | Accepted | AI Copilot as a small tool-using agent (v0.9) |
| [0010](0010-plugin-marketplace.md) | Accepted | Plugin marketplace v0 — catalogue without sandbox (v0.9) |
