"""Cross-entity search over decisions and tasks.

Implementation note — we deliberately use a portable ``LIKE``-based scan
rather than dialect-specific FTS. Why:

  * The corpus per team is tiny (months → low thousands of rows).
  * Setting up SQLite FTS5 vs. Postgres ``to_tsvector`` requires conditional
    DDL we'd rather not maintain right now.
  * The simple scorer (term frequency + field boosts) is good enough for
    the "find me what we decided about X" use case the product targets.

If a team needs heavier search, this module is the only one to swap.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from . import models

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


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


def _score(haystack: str, query_tokens: Iterable[str], *, field_boost: float) -> float:
    htoks = _tokens(haystack)
    if not htoks:
        return 0.0
    htf: dict[str, int] = {}
    for t in htoks:
        htf[t] = htf.get(t, 0) + 1
    score = 0.0
    for q in query_tokens:
        score += htf.get(q, 0)
    return score * field_boost


def search(
    sess: Session, *, team_id: int, query: str, limit: int = 25
) -> list[SearchHit]:
    q = (query or "").strip()
    if not q:
        return []
    q_tokens = _tokens(q)
    if not q_tokens:
        return []
    like_clauses = [f"%{tok}%" for tok in q_tokens]

    # Pull narrow candidate sets via OR of LIKEs to keep memory bounded.
    decision_q = select(models.Decision).where(models.Decision.team_id == team_id)
    decision_q = decision_q.where(
        or_(*[models.Decision.statement.ilike(c) for c in like_clauses],
            *[models.Decision.rationale.ilike(c) for c in like_clauses])
    )
    decisions = list(sess.execute(decision_q).scalars())

    task_q = select(models.Task).where(models.Task.team_id == team_id)
    task_q = task_q.where(
        or_(*[models.Task.title.ilike(c) for c in like_clauses],
            *[models.Task.description.ilike(c) for c in like_clauses])
    )
    tasks = list(sess.execute(task_q).scalars())

    hits: list[SearchHit] = []
    for d in decisions:
        s = _score(d.statement, q_tokens, field_boost=2.0) + \
            _score(d.rationale or "", q_tokens, field_boost=1.0)
        if s > 0:
            hits.append(SearchHit(
                kind="decision", id=d.id, title=d.statement[:120],
                snippet=(d.rationale or d.statement)[:240],
                score=s, meeting_id=d.meeting_id,
            ))
    for t in tasks:
        s = _score(t.title, q_tokens, field_boost=2.0) + \
            _score(t.description or "", q_tokens, field_boost=1.0)
        if s > 0:
            hits.append(SearchHit(
                kind="task", id=t.id, title=t.title,
                snippet=(t.description or t.title)[:240],
                score=s, meeting_id=t.meeting_id,
            ))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]
