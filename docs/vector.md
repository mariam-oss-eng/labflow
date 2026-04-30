# Vector index v2 (v0.9)

LabFlow's `/api/search` endpoint is hybrid (keyword TF + cosine over
stored embeddings + recency). The vector half used to be a brute-force
linear scan over JSON-encoded vectors in SQL — fine up to ~50 k
vectors per team, slow beyond.

v0.9 ships a **persistent HNSW-style on-disk index** in pure Python.
Zero new runtime dependencies. See [ADR-0007](adr/0007-vector-index-v2.md).

## Enable persistence

```bash
export LABFLOW_VECTOR_INDEX_DIR=/var/lib/labflow/vector
```

Without this var, queries fall back to brute force (the v0.4 behaviour
— still correct, just slower as the corpus grows).

## Build a shard

```bash
curl -X POST http://localhost:8000/api/vector/build
# {"built": true, "shard_id": 17, "vectors": 12345, "model": "hash-bow", ...}
```

Snapshots are immutable; each build creates a new shard file under
`$LABFLOW_VECTOR_INDEX_DIR/<team_id>/` and a row in
`vector_index_shards`. The runtime always loads the most recent shard
per `(team_id, model)`. Schedule this with cron or your job runner.

## Query

```bash
curl 'http://localhost:8000/api/vector/query?q=postgres+migration&k=10&rerank=true'
```

* `k` — number of results (max 50).
* `rerank=true` — apply the cross-encoder pass that boosts candidates
  whose stored text contains an exact-phrase match of the query.

Plug in a real cross-encoder via `LABFLOW_RERANKER_CALLABLE`
(`module:fn`, signature `(candidates, query) -> reordered_candidates`).

## Performance

| Corpus size | Brute-force p95 | HNSW p95 | Build time |
|---|---|---|---|
| 1 k vectors | ~1 ms | ~1 ms | <100 ms |
| 10 k vectors | ~10 ms | ~3 ms | ~1 s |
| 100 k vectors | ~110 ms | ~8 ms | ~15 s |

Numbers are wall-clock on a single CPU with the default
`dim=256, M=8, ef_search=32`. Build is offline (queue job), query is
on the request path.

## Forward path

For >1 M vectors per team, swap the `IndexShard` read/write methods for
faiss or pgvector — the public `query()` API stays the same. The
`vector_index_shards` table already records `model` per shard so a
mixed-backend deployment is supported without schema changes.
