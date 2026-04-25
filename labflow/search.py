"""Cross-entity search over decisions and tasks (v0.4 hybrid edition).

We combine three signals:

  * **Lexical / TF**  — token-frequency scorer with field boosts
    (``title × 2`` + ``description × 1``). Computed against ``LIKE``
    candidates so memory is bounded.
  * **Semantic** — cosine similarity between the query embedding and
    each candidate's stored vector (see :mod:`labflow.embeddings`).
    Falls back to ``0.0`` if no embedding has been computed for the
    candidate (e.g. the embedder model has changed).
  * **Recency** — gentle bias toward newer rows (decay ``0.5`` per
    180 days) so old superseded decisions don't drown out recent ones.

Final score = ``α · semantic + (1-α) · lexical_norm + 0.05 · recency``,
with ``α`` configurable via ``LABFLOW_SEARCH_ALPHA``. This matches the
"hybrid retrieval" pattern that vector-search providers (Weaviate,
pgvector) recommend; the heavy machinery just isn't needed here.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import embedding_store, models
from .config import get_settings
from .embeddings import Vector, cosine, get_embedder
from .time_utils import now_utc

_TOKEN = re.compile(r"[a-z0-9]{3,}")
EntityKind = Literal["decision", "task"]


@dataclass
class SearchHit:
    kind: EntityKind
    id: int
    title: str
    snippet: str
    score: float
    meeting_id: int | None
    # Component scores for debugging / explainability.
    score_components: dict[str, float] | None = None


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


def _lex_score(haystack: str, query_tokens: Iterable[str], *, field_boost: float) -> float:
    htoks = _tokens(haystack)
    if not htoks:
        return 0.0
    htf: dict[str, int] = {}
    for t in htoks:
        htf[t] = htf.get(t, 0) + 1
    return sum(htf.get(q, 0) for q in query_tokens) * field_boost


def _recency(then: datetime | None) -> float:
    """Half-life decay over 180 days, so recent rows score ~1.0 and
    year-old rows score ~0.25."""
    if then is None:
        return 0.0
    now_naive = now_utc().replace(tzinfo=None)
    # ``then`` from the DB is tz-naive; if a caller hands us tz-aware,
    # normalize to compare safely.
    if getattr(then, "tzinfo", None) is not None:
        then = then.replace(tzinfo=None)
    age_days = max((now_naive - then).days, 0)
    return float(0.5 ** (age_days / 180.0))


def search(
    sess: Session,
    *,
    team_id: int,
    query: str,
    limit: int = 25,
    alpha: float | None = None,
) -> list[SearchHit]:
    q = (query or "").strip()
    if not q:
        return []
    q_tokens = _tokens(q)
    if not q_tokens:
        return []

    settings = get_settings()
    if alpha is None:
        alpha = float(getattr(settings, "search_alpha", 0.5))
    alpha = max(0.0, min(1.0, alpha))

    embedder = get_embedder()
    query_vec: Vector | None
    try:
        query_vec = embedder.embed(q)
    except Exception:  # noqa: BLE001 — never break search on embedder error
        query_vec = None

    like_clauses = [f"%{tok}%" for tok in q_tokens]

    # --- Candidate sets via LIKE OR semantic top-K -------------------------
    # We pull lexical candidates with LIKE, plus the top semantic neighbors
    # so paraphrased matches surface even when no token literally appears.
    decisions = list(
        sess.execute(
            select(models.Decision)
            .where(models.Decision.team_id == team_id)
            .where(or_(
                *[models.Decision.statement.ilike(c) for c in like_clauses],
                *[models.Decision.rationale.ilike(c) for c in like_clauses],
            ))
        ).scalars()
    )
    tasks = list(
        sess.execute(
            select(models.Task)
            .where(models.Task.team_id == team_id)
            .where(or_(
                *[models.Task.title.ilike(c) for c in like_clauses],
                *[models.Task.description.ilike(c) for c in like_clauses],
            ))
        ).scalars()
    )

    # Stitch in semantic top-K from the embedding table.
    if query_vec is not None:
        d_vecs = embedding_store.load_vectors(
            sess, team_id=team_id, entity_type="decision", model=embedder.name
        )
        t_vecs = embedding_store.load_vectors(
            sess, team_id=team_id, entity_type="task", model=embedder.name
        )

        def _topk(vmap: dict[int, Vector], k: int = 25) -> set[int]:
            scored = [(eid, cosine(query_vec, v)) for eid, v in vmap.items()]
            scored.sort(key=lambda x: x[1], reverse=True)
            return {eid for eid, s in scored[:k] if s > 0.05}

        d_ids = _topk(d_vecs)
        t_ids = _topk(t_vecs)
        existing_d = {d.id for d in decisions}
        existing_t = {t.id for t in tasks}
        if d_ids - existing_d:
            decisions += list(
                sess.execute(
                    select(models.Decision).where(
                        models.Decision.team_id == team_id,
                        models.Decision.id.in_(d_ids - existing_d),
                    )
                ).scalars()
            )
        if t_ids - existing_t:
            tasks += list(
                sess.execute(
                    select(models.Task).where(
                        models.Task.team_id == team_id,
                        models.Task.id.in_(t_ids - existing_t),
                    )
                ).scalars()
            )
    else:
        d_vecs, t_vecs = {}, {}

    # --- Score & combine ---------------------------------------------------
    hits: list[SearchHit] = []

    def _add_lex(text_for_lex: str) -> float:
        return (
            _lex_score(text_for_lex, q_tokens, field_boost=2.0)
        )

    # Find max lexical to normalize.
    lex_raw_d = {
        d.id: _lex_score(d.statement, q_tokens, field_boost=2.0)
              + _lex_score(d.rationale or "", q_tokens, field_boost=1.0)
        for d in decisions
    }
    lex_raw_t = {
        t.id: _lex_score(t.title, q_tokens, field_boost=2.0)
              + _lex_score(t.description or "", q_tokens, field_boost=1.0)
        for t in tasks
    }
    max_lex = max([0.0] + list(lex_raw_d.values()) + list(lex_raw_t.values())) or 1.0

    for d in decisions:
        sem = cosine(query_vec, d_vecs[d.id]) if (query_vec and d.id in d_vecs) else 0.0
        sem = max(0.0, sem)
        lex = lex_raw_d[d.id] / max_lex
        rec = _recency(d.created_at)
        score = alpha * sem + (1.0 - alpha) * lex + 0.05 * rec
        if score <= 0.0:
            continue
        hits.append(SearchHit(
            kind="decision", id=d.id, title=d.statement[:120],
            snippet=(d.rationale or d.statement)[:240],
            score=score, meeting_id=d.meeting_id,
            score_components={"semantic": sem, "lexical": lex, "recency": rec},
        ))
    for t in tasks:
        sem = cosine(query_vec, t_vecs[t.id]) if (query_vec and t.id in t_vecs) else 0.0
        sem = max(0.0, sem)
        lex = lex_raw_t[t.id] / max_lex
        rec = _recency(t.created_at)
        score = alpha * sem + (1.0 - alpha) * lex + 0.05 * rec
        if score <= 0.0:
            continue
        hits.append(SearchHit(
            kind="task", id=t.id, title=t.title,
            snippet=(t.description or t.title)[:240],
            score=score, meeting_id=t.meeting_id,
            score_components={"semantic": sem, "lexical": lex, "recency": rec},
        ))

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]

