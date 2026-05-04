"""Watchers and activity feed (v0.11).

A *watcher* is a per-API-key subscription to entity changes. When an
audit row is appended for a watched ``(entity_type, entity_id)`` pair,
the watcher's API key shows that event in its personal feed (and, for
``email``/``slack`` delivery channels, would be queued for outbound
delivery — left as a hook so this module ships without new dependencies).

The activity feed itself is computed at read time by joining
``watchers`` and ``audit_events`` — no additional persistence needed,
the audit log is already the source of truth.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from . import models
from .errors import ConflictError, NotFoundError, ValidationError

VALID_ENTITY_TYPES = {
    "task", "decision", "meeting", "wiki", "experiment", "sprint",
    "blocker", "assumption", "evidence",
}
VALID_DELIVERIES = {"feed", "email", "slack"}


def add_watch(
    sess: Session, *, team_id: int, api_key_id: int,
    entity_type: str, entity_id: int, delivery: str = "feed",
) -> models.Watcher:
    if entity_type not in VALID_ENTITY_TYPES:
        raise ValidationError(
            f"unsupported entity_type {entity_type!r}; valid: "
            f"{sorted(VALID_ENTITY_TYPES)}"
        )
    if delivery not in VALID_DELIVERIES:
        raise ValidationError(f"invalid delivery {delivery!r}")
    existing = sess.execute(
        select(models.Watcher).where(
            models.Watcher.team_id == team_id,
            models.Watcher.api_key_id == api_key_id,
            models.Watcher.entity_type == entity_type,
            models.Watcher.entity_id == entity_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.delivery = delivery
        sess.flush()
        return existing
    w = models.Watcher(
        team_id=team_id, api_key_id=api_key_id,
        entity_type=entity_type, entity_id=entity_id,
        delivery=delivery,
    )
    sess.add(w)
    sess.flush()
    return w


def remove_watch(
    sess: Session, *, team_id: int, api_key_id: int,
    entity_type: str, entity_id: int,
) -> bool:
    existing = sess.execute(
        select(models.Watcher).where(
            models.Watcher.team_id == team_id,
            models.Watcher.api_key_id == api_key_id,
            models.Watcher.entity_type == entity_type,
            models.Watcher.entity_id == entity_id,
        )
    ).scalar_one_or_none()
    if existing is None:
        return False
    sess.delete(existing)
    sess.flush()
    return True


def list_watches(
    sess: Session, *, team_id: int, api_key_id: int,
) -> list[models.Watcher]:
    return list(sess.execute(
        select(models.Watcher).where(
            models.Watcher.team_id == team_id,
            models.Watcher.api_key_id == api_key_id,
        ).order_by(models.Watcher.id.asc())
    ).scalars())


def feed(
    sess: Session, *, team_id: int, api_key_id: int | None = None,
    since: datetime | None = None, limit: int = 50,
) -> list[dict]:
    """Recent audit events for entities the key watches.

    If ``api_key_id`` is None, returns *all* events for the team
    (admin-style global feed).
    """
    limit = max(1, min(int(limit), 200))
    if api_key_id is None:
        q = select(models.AuditEvent).where(
            models.AuditEvent.team_id == team_id
        )
        if since is not None:
            q = q.where(models.AuditEvent.created_at >= since)
        rows = sess.execute(
            q.order_by(models.AuditEvent.id.desc()).limit(limit)
        ).scalars().all()
        return [_event_dict(r) for r in rows]

    watches = list_watches(sess, team_id=team_id, api_key_id=api_key_id)
    if not watches:
        return []
    # Build (entity_type, entity_id) tuples and produce a single OR.
    clauses = [
        and_(
            models.AuditEvent.entity_type == w.entity_type,
            models.AuditEvent.entity_id == w.entity_id,
        )
        for w in watches
    ]
    q = select(models.AuditEvent).where(
        models.AuditEvent.team_id == team_id, or_(*clauses)
    )
    if since is not None:
        q = q.where(models.AuditEvent.created_at >= since)
    rows = sess.execute(
        q.order_by(models.AuditEvent.id.desc()).limit(limit)
    ).scalars().all()
    return [_event_dict(r) for r in rows]


def _event_dict(evt: models.AuditEvent) -> dict[str, Any]:
    return {
        "id": evt.id,
        "actor": evt.actor,
        "action": evt.action,
        "entity_type": evt.entity_type,
        "entity_id": evt.entity_id,
        "created_at": evt.created_at.isoformat(),
        "metadata_json": evt.metadata_json,
    }
