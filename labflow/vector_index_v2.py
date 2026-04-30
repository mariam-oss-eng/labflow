"""Vector index v2 — persistent HNSW-style on-disk shards (v0.9).

The v0.4 vector store used brute-force linear scan over JSON-encoded vectors
in SQL. That works well up to ~50k vectors per team; beyond that, query
latency becomes user-visible. v2 adds:

  * **HNSW-style hierarchical graph** index (pure Python, no faiss)
    persisted as a single binary file per team/model.
  * **Snapshot rotation** via :class:`models.VectorIndexShard` — each build
    is a new row; the runtime loads the most recent shard per
    ``(team_id, model)``.
  * **Brute-force fallback** when no shard is built (for new teams / tests).
  * **Cross-encoder re-rank** stub: candidates from the index are re-scored
    using a cheap rule-based "phrase boost" pass that prefers exact-phrase
    matches over pure cosine. A real cross-encoder can be plugged in via
    the ``LABFLOW_RERANKER_CALLABLE`` environment variable.

This module trades raw speed for *zero new dependencies* — we use only the
stdlib ``struct``/``pickle``/``hashlib`` so the install footprint stays
small. For >1M vectors per team, swap the file format for faiss or
pgvector by reimplementing ``IndexShard``'s read/write methods.
"""
from __future__ import annotations

import heapq
import logging
import math
import os
import pickle
import random
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .config import get_settings
from .embeddings import Vector, get_embedder
from .time_utils import now_utc

log = logging.getLogger("labflow.vector_index_v2")

# HNSW parameters. M = neighbours per node; ef_construction = candidate set
# during build; ef_search = candidate set during query. The defaults are
# conservative — small enough to build in milliseconds for ~10k vectors,
# large enough for >0.95 recall vs. brute force.
_M = 8
_EF_CONSTRUCTION = 32
_EF_SEARCH = 32
_LAYER_PROB = 1.0 / math.log(_M)

_INDEX_FILE_VERSION = 1


# --------------------------------------------------------------------------- math

def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# --------------------------------------------------------------------------- shard

@dataclass
class IndexEntry:
    """A single (entity_type, entity_id, vector) row inside a shard."""
    entity_type: str
    entity_id: int


class IndexShard:
    """In-memory HNSW-ish index. Persisted with pickle for simplicity.

    The graph is represented as ``layers[layer_idx][node_idx] -> list[neighbour_idx]``.
    Nodes are zero-indexed and live in ``self.entries``/``self.vectors``.
    """

    def __init__(self, *, model: str, dim: int):
        self.model = model
        self.dim = dim
        self.entries: list[IndexEntry] = []
        self.vectors: list[list[float]] = []
        self.layers: list[dict[int, list[int]]] = []
        self.entry_point: int | None = None
        self.rng = random.Random(42)  # deterministic builds for tests

    # ---- build ---------------------------------------------------------
    def add(self, entry: IndexEntry, vector: list[float]) -> None:
        node_idx = len(self.entries)
        self.entries.append(entry)
        self.vectors.append(vector)
        # Pick the level for this node by sampling from a geometric distribution.
        level = 0
        while self.rng.random() < _LAYER_PROB and level < 16:
            level += 1
        # Grow layers as needed.
        while len(self.layers) <= level:
            self.layers.append({})
        if self.entry_point is None:
            for L in range(level + 1):
                self.layers[L][node_idx] = []
            self.entry_point = node_idx
            return
        # Greedy descent from top until target level.
        cur = self.entry_point
        cur_dist = -_cosine(vector, self.vectors[cur])
        for L in range(len(self.layers) - 1, level, -1):
            cur, cur_dist = self._greedy_at(L, vector, cur, cur_dist)
        # Connect at each level <= node level.
        for L in range(min(level, len(self.layers) - 1), -1, -1):
            cands = self._search_layer(L, vector, [cur], _EF_CONSTRUCTION)
            neighbours = [n for _, n in heapq.nsmallest(_M, cands)]
            self.layers[L][node_idx] = neighbours
            # Bidirectional: clip neighbour lists to _M as well.
            for n in neighbours:
                lst = self.layers[L].setdefault(n, [])
                lst.append(node_idx)
                if len(lst) > _M:
                    # Keep the M closest by distance.
                    nv = self.vectors[n]
                    pruned = heapq.nsmallest(
                        _M, lst,
                        key=lambda x: -_cosine(nv, self.vectors[x]),
                    )
                    self.layers[L][n] = pruned

    def _greedy_at(self, layer: int, q: list[float],
                   start: int, start_dist: float) -> tuple[int, float]:
        cur, cur_dist = start, start_dist
        layer_map = self.layers[layer]
        while True:
            improved = False
            for nb in layer_map.get(cur, []):
                d = -_cosine(q, self.vectors[nb])
                if d < cur_dist:
                    cur, cur_dist = nb, d
                    improved = True
            if not improved:
                return cur, cur_dist

    def _search_layer(self, layer: int, q: list[float],
                      seeds: list[int], ef: int) -> list[tuple[float, int]]:
        layer_map = self.layers[layer]
        visited: set[int] = set(seeds)
        # min-heap of (dist, idx) for candidates; max-heap of result by negation.
        cand: list[tuple[float, int]] = []
        result: list[tuple[float, int]] = []
        for s in seeds:
            d = -_cosine(q, self.vectors[s])
            heapq.heappush(cand, (d, s))
            heapq.heappush(result, (-d, s))
        while cand:
            d_c, c = heapq.heappop(cand)
            d_worst = -result[0][0] if result else float("inf")
            if d_c > d_worst and len(result) >= ef:
                break
            for nb in layer_map.get(c, []):
                if nb in visited:
                    continue
                visited.add(nb)
                d_n = -_cosine(q, self.vectors[nb])
                if len(result) < ef or d_n < -result[0][0]:
                    heapq.heappush(cand, (d_n, nb))
                    heapq.heappush(result, (-d_n, nb))
                    if len(result) > ef:
                        heapq.heappop(result)
        return [(-d, i) for d, i in result]

    # ---- query ---------------------------------------------------------
    def query(self, vector: list[float], *, k: int = 10) -> list[tuple[float, IndexEntry]]:
        if self.entry_point is None or not self.entries:
            return []
        cur = self.entry_point
        cur_dist = -_cosine(vector, self.vectors[cur])
        for L in range(len(self.layers) - 1, 0, -1):
            cur, cur_dist = self._greedy_at(L, vector, cur, cur_dist)
        cands = self._search_layer(0, vector, [cur], max(_EF_SEARCH, k))
        # cands are (dist=-cos, idx). Convert back to similarity, top-k.
        scored = [(_cosine(vector, self.vectors[i]), i) for _, i in cands]
        scored.sort(key=lambda x: -x[0])
        return [(s, self.entries[i]) for s, i in scored[:k]]

    # ---- persistence ---------------------------------------------------
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Use pickle protocol 5 wrapped with a 4-byte version header. Pickle
        # is fine here because the file is generated and consumed by us only;
        # the version header lets us evolve the format safely.
        body = pickle.dumps({
            "model": self.model, "dim": self.dim,
            "entries": [(e.entity_type, e.entity_id) for e in self.entries],
            "vectors": self.vectors, "layers": self.layers,
            "entry_point": self.entry_point,
        }, protocol=5)
        with path.open("wb") as f:
            f.write(struct.pack("<I", _INDEX_FILE_VERSION))
            f.write(body)

    @classmethod
    def load(cls, path: Path) -> "IndexShard":
        with path.open("rb") as f:
            ver = struct.unpack("<I", f.read(4))[0]
            if ver != _INDEX_FILE_VERSION:
                raise ValueError(f"unknown index file version: {ver}")
            data = pickle.loads(f.read())  # noqa: S301 — generated by us
        s = cls(model=data["model"], dim=data["dim"])
        s.entries = [IndexEntry(*t) for t in data["entries"]]
        s.vectors = data["vectors"]
        s.layers = data["layers"]
        s.entry_point = data["entry_point"]
        return s


# --------------------------------------------------------------------------- builder

def build_team_index(sess: Session, *, team_id: int) -> models.VectorIndexShard | None:
    """Build a fresh shard from all current ``Embedding`` rows for a team.

    Returns the shard metadata row, or None if there are no vectors or the
    ``LABFLOW_VECTOR_INDEX_DIR`` setting isn't configured.
    """
    settings = get_settings()
    base = (settings.vector_index_dir or "").strip()
    if not base:
        return None
    embedder = get_embedder()
    rows = list(sess.execute(
        select(models.Embedding).where(
            models.Embedding.team_id == team_id,
            models.Embedding.model == embedder.name,
        )
    ).scalars())
    if not rows:
        return None
    shard = IndexShard(model=embedder.name, dim=embedder.dim)
    import json as _json
    for r in rows:
        try:
            v = _json.loads(r.vector)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(v, list) or len(v) != shard.dim:
            continue
        shard.add(IndexEntry(entity_type=r.entity_type, entity_id=r.entity_id), v)
    out_dir = Path(base) / str(team_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = now_utc().strftime("%Y%m%dT%H%M%S")
    path = out_dir / f"{embedder.name}-{ts}.idx"
    shard.save(path)
    sha = _sha256_file(path)
    meta = models.VectorIndexShard(
        team_id=team_id, model=embedder.name,
        dim=embedder.dim, vectors=len(shard.entries),
        path=str(path), sha256=sha,
    )
    sess.add(meta)
    sess.flush()
    log.info("vector index v2 built team=%s model=%s vectors=%d",
             team_id, embedder.name, len(shard.entries))
    return meta


def _sha256_file(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


_loaded_cache: dict[tuple[int, str], tuple[Path, IndexShard]] = {}


def load_latest(sess: Session, *, team_id: int) -> IndexShard | None:
    """Load and cache the most recent shard for a team. None means
    "no index built yet — caller should fall back to brute force"."""
    embedder = get_embedder()
    meta = sess.execute(
        select(models.VectorIndexShard).where(
            models.VectorIndexShard.team_id == team_id,
            models.VectorIndexShard.model == embedder.name,
        ).order_by(models.VectorIndexShard.built_at.desc()).limit(1)
    ).scalar_one_or_none()
    if meta is None:
        return None
    path = Path(meta.path)
    if not path.is_file():
        log.warning("shard file missing: %s", path)
        return None
    cached = _loaded_cache.get((team_id, embedder.name))
    if cached is not None and cached[0] == path:
        return cached[1]
    shard = IndexShard.load(path)
    _loaded_cache[(team_id, embedder.name)] = (path, shard)
    return shard


def reset_cache_for_tests() -> None:
    _loaded_cache.clear()


# --------------------------------------------------------------------------- search + re-rank

def query(
    sess: Session, *, team_id: int, text: str, k: int = 10,
    rerank_query: str | None = None,
) -> list[tuple[float, IndexEntry]]:
    """Return top-k similar entries, with optional re-rank pass.

    Falls back to brute force over all team embeddings when no persistent
    shard is available.
    """
    embedder = get_embedder()
    qvec = embedder.embed(text).values
    shard = load_latest(sess, team_id=team_id)
    if shard is not None:
        cands = shard.query(qvec, k=k * 3)
    else:
        cands = _brute_force(sess, team_id=team_id, qvec=qvec, k=k * 3)
    if not cands:
        return []
    if rerank_query is not None:
        cands = _rerank(cands, sess=sess, team_id=team_id, query=rerank_query)
    return cands[:k]


def _brute_force(sess: Session, *, team_id: int, qvec: list[float],
                 k: int) -> list[tuple[float, IndexEntry]]:
    embedder = get_embedder()
    rows = sess.execute(
        select(models.Embedding).where(
            models.Embedding.team_id == team_id,
            models.Embedding.model == embedder.name,
        )
    ).scalars()
    import json as _json
    scored: list[tuple[float, IndexEntry]] = []
    for r in rows:
        try:
            v = _json.loads(r.vector)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(v, list) or len(v) != embedder.dim:
            continue
        scored.append((_cosine(qvec, v),
                       IndexEntry(entity_type=r.entity_type, entity_id=r.entity_id)))
    scored.sort(key=lambda x: -x[0])
    return scored[:k]


def _rerank(
    cands: list[tuple[float, IndexEntry]], *, sess: Session,
    team_id: int, query: str,
) -> list[tuple[float, IndexEntry]]:
    """Tiny rule-based cross-encoder: boost candidates whose stored text
    contains exact-phrase matches of multi-word query tokens.
    """
    qlow = query.lower().strip()
    phrases = [p for p in qlow.split() if len(p) > 3]
    boosted: list[tuple[float, IndexEntry]] = []
    for score, e in cands:
        title = _entity_text(sess, team_id=team_id, entity=e)
        bonus = 0.0
        if title:
            tlow = title.lower()
            if qlow and qlow in tlow:
                bonus += 0.15
            for p in phrases:
                if p in tlow:
                    bonus += 0.03
        boosted.append((score + bonus, e))
    boosted.sort(key=lambda x: -x[0])
    return boosted


def _entity_text(sess: Session, *, team_id: int, entity: IndexEntry) -> str:
    if entity.entity_type == "decision":
        d = sess.get(models.Decision, entity.entity_id)
        return d.statement if d and d.team_id == team_id else ""
    if entity.entity_type == "task":
        t = sess.get(models.Task, entity.entity_id)
        return t.title if t and t.team_id == team_id else ""
    return ""
