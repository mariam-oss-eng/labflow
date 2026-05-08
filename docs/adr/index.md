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
| [0011](0011-tamper-evident-audit-chain.md) | Accepted | Tamper-evident audit chain (v0.10) |
| [0012](0012-automation-rules.md) | Accepted | Declarative automation rules engine (v0.10) |
| [0013](0013-wiki-and-smart-links.md) | Accepted | Wiki + smart entity links (v0.11) |
| [0014](0014-graphql-mutations.md) | Accepted | GraphQL mutations on the hand-rolled engine (v0.11) |
| [0015](0015-recurring-tasks.md) | Accepted | Recurring tasks live in the same job queue (v0.12) |
| [0016](0016-api-key-quotas.md) | Accepted | Per-API-key daily quotas (v0.12) |
| [0017](0017-federated-invites.md) | Accepted | Federated guest invites with scoped ACLs (v0.13) |
| [0018](0018-smart-lists.md) | Accepted | Smart lists are declarative, not query DSL (v0.13) |
