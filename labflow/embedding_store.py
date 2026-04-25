"""Helpers for storing and looking up :class:`Embedding` rows.

We keep this layer tiny — embedding rows are managed *eagerly* by
``services.persist_extraction`` whenever decisions or tasks change.
There is no background "reindex" job; vectors are recomputed in line
with writes so ``/api/search`` always sees a consistent view.
"""
from __future__ import annotations

from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .embeddings import Vector, get_embedder


def upsert_vector(
    sess: Session,
    *,
    team_id: int,
    entity_type: str,
    entity_id: int,
    text: str,
) -> models.Embedding:
    """Compute and persist an embedding for an entity. Idempotent on
    ``(team_id, entity_type, entity_id, model)``.
    """
    embedder = get_embedder()
    vec = embedder.embed(text)
    existing = sess.execute(
        select(models.Embedding).where(
            models.Embedding.team_id == team_id,
            models.Embedding.entity_type == entity_type,
            models.Embedding.entity_id == entity_id,
            models.Embedding.model == embedder.name,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.vector = vec.to_json()
        existing.dim = vec.dim
        return existing
    row = models.Embedding(
        team_id=team_id, entity_type=entity_type, entity_id=entity_id,
        model=embedder.name, dim=vec.dim, vector=vec.to_json(),
    )
    sess.add(row)
    sess.flush()
    return row


def delete_for(
    sess: Session, *, team_id: int, entity_type: str, entity_id: int
) -> None:
    rows = sess.execute(
        select(models.Embedding).where(
            models.Embedding.team_id == team_id,
            models.Embedding.entity_type == entity_type,
            models.Embedding.entity_id == entity_id,
        )
    ).scalars().all()
    for r in rows:
        sess.delete(r)


def load_vectors(
    sess: Session, *, team_id: int, entity_type: str, model: str
) -> dict[int, Vector]:
    """Return ``{entity_id: Vector}`` for the requested entity type and model."""
    rows = sess.execute(
        select(models.Embedding).where(
            models.Embedding.team_id == team_id,
            models.Embedding.entity_type == entity_type,
            models.Embedding.model == model,
        )
    ).scalars().all()
    return {r.entity_id: Vector.from_json(r.vector) for r in rows}
