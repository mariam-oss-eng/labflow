"""Saved searches (v0.6).

A saved search is a named, persistent ``/api/search`` query bound to a
team. They appear in the dashboard sidebar and can be pinned. Slugs are
auto-generated from the name when the caller doesn't pass one.
"""
from __future__ import annotations

import json
import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .errors import ConflictError, NotFoundError, ValidationError

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Best-effort URL-safe slug. Pure stdlib so we don't add a dep."""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = _SLUG_RE.sub("-", n.lower()).strip("-")
    return s[:64] or "search"


def create(
    sess: Session,
    *,
    team_id: int,
    name: str,
    query: str,
    slug: str | None = None,
    alpha: float | None = None,
    filters: dict | None = None,
    pinned: bool = False,
) -> models.SavedSearch:
    name = (name or "").strip()
    query = (query or "").strip()
    if not name or not query:
        raise ValidationError("name and query are required")
    slug = slugify(slug or name)
    if sess.execute(
        select(models.SavedSearch).where(
            models.SavedSearch.team_id == team_id,
            models.SavedSearch.slug == slug,
        )
    ).scalar_one_or_none() is not None:
        raise ConflictError(f"saved search with slug {slug!r} already exists")
    row = models.SavedSearch(
        team_id=team_id, slug=slug, name=name, query=query,
        alpha=alpha, filters=json.dumps(filters) if filters else None,
        pinned=pinned,
    )
    sess.add(row)
    sess.flush()
    return row


def list_for(sess: Session, *, team_id: int) -> list[models.SavedSearch]:
    return list(
        sess.execute(
            select(models.SavedSearch)
            .where(models.SavedSearch.team_id == team_id)
            .order_by(models.SavedSearch.pinned.desc(), models.SavedSearch.name)
        ).scalars()
    )


def get(sess: Session, *, team_id: int, slug: str) -> models.SavedSearch:
    row = sess.execute(
        select(models.SavedSearch).where(
            models.SavedSearch.team_id == team_id,
            models.SavedSearch.slug == slug,
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"saved search {slug!r} not found")
    return row


def delete(sess: Session, *, team_id: int, slug: str) -> None:
    row = get(sess, team_id=team_id, slug=slug)
    sess.delete(row)
    sess.flush()


def to_dict(row: models.SavedSearch) -> dict:
    return {
        "id": row.id, "slug": row.slug, "name": row.name,
        "query": row.query, "alpha": row.alpha,
        "filters": json.loads(row.filters) if row.filters else None,
        "pinned": row.pinned,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
