"""PostgreSQL full-text search backend (v0.7).

When the configured database is PostgreSQL, the lexical layer of
``labflow.search`` upgrades from ``ILIKE`` candidate fetching to
``to_tsvector @@ plainto_tsquery``, which is dramatically faster on
big corpora and respects English stemming, stop words, and ranking.

Usage
-----
``maybe_pg_search`` returns ``(decisions, tasks)`` candidate lists when
the engine is PostgreSQL, else ``None``. The hybrid ``search.search``
function falls back to its current ``ILIKE`` path when this returns
``None`` so SQLite users see no behavior change.
"""
from __future__ import annotations

import logging
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import models

log = logging.getLogger("labflow.pg_fts")


def _is_postgres(sess: Session) -> bool:
    try:
        return sess.bind.dialect.name == "postgresql"
    except Exception:
        return False


def _pg_query(tokens: Iterable[str]) -> str:
    """Build a tsquery string. We OR the user's tokens for recall, escape
    by quoting (postgres tsquery doesn't allow stray punctuation)."""
    safe = [t.replace("'", "").replace("&", "").replace("|", "")
            for t in tokens if t.strip()]
    safe = [t for t in safe if t]
    return " | ".join(safe) if safe else ""


def maybe_pg_search(
    sess: Session, *, team_id: int, tokens: list[str], limit: int = 50,
) -> tuple[list[models.Decision], list[models.Task]] | None:
    """Return PG-FTS candidates or None if not on Postgres."""
    if not _is_postgres(sess):
        return None
    q = _pg_query(tokens)
    if not q:
        return [], []
    # We expect operators to have created the matching GIN indexes; if not,
    # the queries still work, just slower.
    decisions = list(
        sess.execute(
            select_text := text(
                "SELECT * FROM decisions "
                "WHERE team_id = :team_id "
                "  AND to_tsvector('english', "
                "         coalesce(statement,'') || ' ' || coalesce(rationale,'')) "
                "      @@ to_tsquery('english', :q) "
                "ORDER BY ts_rank(to_tsvector('english', "
                "        coalesce(statement,'') || ' ' || coalesce(rationale,'')), "
                "        to_tsquery('english', :q)) DESC "
                "LIMIT :lim"
            ),
            {"team_id": team_id, "q": q, "lim": limit},
        )
        .mappings()
    )
    tasks = list(
        sess.execute(
            text(
                "SELECT * FROM tasks "
                "WHERE team_id = :team_id "
                "  AND to_tsvector('english', "
                "         coalesce(title,'') || ' ' || coalesce(description,'')) "
                "      @@ to_tsquery('english', :q) "
                "ORDER BY ts_rank(to_tsvector('english', "
                "        coalesce(title,'') || ' ' || coalesce(description,'')), "
                "        to_tsquery('english', :q)) DESC "
                "LIMIT :lim"
            ),
            {"team_id": team_id, "q": q, "lim": limit},
        )
        .mappings()
    )
    # Hydrate via primary keys so the existing scoring code still gets ORM rows.
    d_ids = [r["id"] for r in decisions]
    t_ids = [r["id"] for r in tasks]
    d_rows = (
        sess.query(models.Decision).filter(models.Decision.id.in_(d_ids)).all()
        if d_ids else []
    )
    t_rows = (
        sess.query(models.Task).filter(models.Task.id.in_(t_ids)).all()
        if t_ids else []
    )
    return d_rows, t_rows
