# ADR-0001 · Hybrid search ranking

* Status: Accepted
* Date: 2026-04-25 (v0.4)

## Context

v0.3 search was a SQL `LIKE` query with a tiny hand-rolled scorer. It
was fine for tiny demos but failed three real-world properties teams
expect from a search box:

1. **Recall on paraphrases.** "tokenizer choice" should find a
   decision that says "we adopted SentencePiece for tokenization."
2. **Recency bias.** Two hits with equal lexical match should sort with
   the newer one on top.
3. **Explainability.** When a result looks wrong, an engineer should
   be able to ask *why* it ranked there.

We also have hard operational constraints: no required external
service (so no Elasticsearch / Pinecone / Weaviate), and no
heavyweight ML dependency in the default install (so no `sentence-transformers`).

## Decision

We ship a **hybrid ranker** that blends three signals per candidate row:

* **Lexical score** — token TF over title + body, with field weighting.
  Stop-words are dropped; case is folded. This gives us BM25-flavoured
  behaviour without an external index.
* **Semantic score** — cosine similarity between the query embedding
  and a per-row vector stored in `embeddings`. The default embedder is
  a deterministic feature-hashing bag-of-words (`HashEmbedder`); teams
  who want true paraphrase recall set `LABFLOW_EMBEDDING_CALLABLE` to
  any `pkg.mod:fn` that returns a vector — we'll use it transparently.
* **Recency score** — half-life decay over 180 days from the row's
  `created_at`. This keeps stale-but-popular results from drowning
  fresh decisions.

The blend is `score = α·semantic + (1-α)·lexical + β·recency`, with α
configurable per query (`?alpha=`) or globally
(`LABFLOW_SEARCH_ALPHA`). Each result returns a `score_components`
dict so an operator can inspect the breakdown.

## Consequences

* **Positive** — single endpoint covers exact-match, paraphrase, and
  recency-biased queries. The default offline path is dependency-free
  and runs against SQLite. Plugging in a real embedding callable
  improves recall *without* changing any client code.
* **Negative** — the offline default is a bag-of-words, so it will
  miss truly synonym-only queries. We document this and ship a one-line
  config to swap in a real embedder.
* **Follow-on** — index size grows ~1KB/row in the default 256-d case;
  retention sweep already covers `audit_events` etc., we'll add an
  embedder-version garbage-collect when we need it.
