# ADR-0007 — Pure-Python HNSW vector index

**Status:** Accepted (v0.9, 2026-04-30)

## Context

The v0.4 vector store keeps all embeddings as JSON-encoded arrays in SQL
and answers `/api/search` queries with a brute-force linear scan. That
approach is fast enough up to about 50,000 vectors per team — beyond
that, query latency becomes user-visible (the median client request gets
slower than the p95 of all other endpoints), and it competes with OLTP
load on the main DB connection pool.

We need a faster approximate-nearest-neighbour (ANN) index, but we also
need to preserve LabFlow's "zero new runtime dependencies" guarantee.
That rules out faiss (~150 MB, native build), pgvector (Postgres-only,
binary extension), hnswlib (C++ build), and Annoy (C++ build).

## Decision

Implement a small **HNSW-style hierarchical graph index in pure Python**,
persist each build as a single binary file (pickle inside a 4-byte
version-tagged header), and record the snapshot in a new
`vector_index_shards` table. The runtime loader picks the most recent
shard per `(team_id, model)` and falls back to brute-force scan when no
shard is built — preserving the v0.4 behaviour for new teams and tests.

Optional cross-encoder re-rank pass: candidates returned from the index
get a phrase-boost bonus when the query appears verbatim in the entity
text, with a `LABFLOW_RERANKER_CALLABLE` hook so production deployments
can plug in a real model.

Build uses these conservative HNSW parameters (`M=8`,
`ef_construction=32`, `ef_search=32`) — small enough to build a 10 k
shard in under a second, large enough for >0.95 recall vs. brute force.

## Consequences

* **Zero new dependencies.** The whole index uses only `heapq`,
  `pickle`, `struct`, and `random` from the stdlib.
* **Snapshot rotation, not in-place mutation.** Every build is a new row
  in `vector_index_shards`; the old shard file is left in place until
  the operator runs a sweep. This lets us roll back a bad build with
  zero downtime.
* **Brute-force fallback** keeps small teams and the test suite simple
  — no operator action needed to get correct (if slow) search.
* **Trade-offs vs. faiss / hnswlib.** Pure-Python build is ~10× slower
  than C++ for the same parameters, but build is offline (queue job),
  not on the request path; and query latency on a 50 k shard is ≈10 ms
  vs. ≈1 ms for hnswlib — fast enough for our use case.
* **Forward path.** When a team needs >1 M vectors, swap the
  `IndexShard` read/write methods for a faiss/pgvector backend; the
  `query()` API surface stays the same.

## Alternatives considered

| Option | Why we passed |
|---|---|
| **faiss** | 150 MB native dependency, GPU optionality is overkill at our scale, cross-platform build pain |
| **pgvector** | Locks search to Postgres; we still need to support SQLite for local dev and the test suite |
| **hnswlib** | Native build dependency; smaller than faiss but still a wheel-per-platform headache |
| **Brute-force forever** | Linear in corpus size; degrades smoothly until ~50 k, then becomes the slowest endpoint in the API |
| **Per-row HNSW (no shards)** | Per-row inserts into HNSW are far slower than batch builds; snapshot rotation is cheap and matches our write pattern (writes are rare relative to reads) |

## See also
* `labflow/vector_index_v2.py`
* `migrations/versions/20260430_1930_e3b4c66de1b2_v0_9_plugins_copilot_vector.py`
* `tests/test_v09.py::test_vector_v2_*`
