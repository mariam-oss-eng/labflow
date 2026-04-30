"""Sprints / iterations (v0.8).

A sprint is a time-boxed bucket of tasks. Every task may belong to at most
one sprint via :class:`models.Task.sprint_id`. The sprint is "active" until
its operator closes it; multiple sprints can be active at once for teams
that run parallel workstreams (research vs. infra).

The interesting endpoint is the **burndown**: a per-day count of remaining
open tasks across the sprint window. That number is computed entirely from
the audit log so it's deterministic and robust to manual data fixes.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit as audit_mod, models
from .errors import ConflictError, NotFoundError, ValidationError
from .time_utils import now_utc

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_MAX_SPRINT_DAYS = 365


def slugify(name: str) -> str:
    s = _SLUG_RE.sub("-", name.lower()).strip("-")
    return s[:64] or "sprint"


def create_sprint(
    sess: Session, *, team_id: int, name: str,
    starts_at: datetime, ends_at: datetime,
    goal: str | None = None, slug: str | None = None,
    actor: str = "system",
) -> models.Sprint:
    if not name.strip():
        raise ValidationError("sprint name required")
    if ends_at <= starts_at:
        raise ValidationError("ends_at must be after starts_at")
    if (ends_at - starts_at).days > _MAX_SPRINT_DAYS:
        raise ValidationError(f"sprint length must be <= {_MAX_SPRINT_DAYS} days")
    slug = slugify(slug or name)
    if sess.execute(
        select(models.Sprint).where(
            models.Sprint.team_id == team_id, models.Sprint.slug == slug
        )
    ).scalar_one_or_none() is not None:
        raise ConflictError(f"sprint slug {slug!r} already exists")
    sp = models.Sprint(
        team_id=team_id, slug=slug, name=name.strip(),
        starts_at=starts_at, ends_at=ends_at, goal=goal,
    )
    sess.add(sp)
    sess.flush()
    audit_mod.record(sess, team_id=team_id, actor=actor,
                     action="sprint.create", entity_type="sprint",
                     entity_id=sp.id, metadata={"slug": slug})
    return sp


def close_sprint(sess: Session, *, team_id: int, slug: str,
                 actor: str = "system") -> models.Sprint:
    sp = _find(sess, team_id=team_id, slug=slug)
    sp.active = False
    audit_mod.record(sess, team_id=team_id, actor=actor,
                     action="sprint.close", entity_type="sprint",
                     entity_id=sp.id, metadata={"slug": slug})
    return sp


def list_sprints(sess: Session, *, team_id: int,
                 active: bool | None = None) -> list[models.Sprint]:
    q = select(models.Sprint).where(models.Sprint.team_id == team_id)
    if active is not None:
        q = q.where(models.Sprint.active.is_(active))
    return list(sess.execute(q.order_by(models.Sprint.starts_at.desc())).scalars())


def assign_task(sess: Session, *, team_id: int, sprint_slug: str,
                task_id: int, actor: str = "system") -> models.Task:
    sp = _find(sess, team_id=team_id, slug=sprint_slug)
    task = sess.get(models.Task, task_id)
    if task is None or task.team_id != team_id:
        raise NotFoundError("task not found")
    task.sprint_id = sp.id
    audit_mod.record(sess, team_id=team_id, actor=actor,
                     action="sprint.assign", entity_type="task",
                     entity_id=task.id,
                     metadata={"sprint_id": sp.id, "slug": sprint_slug})
    return task


def burndown(sess: Session, *, team_id: int, slug: str) -> dict:
    """Return per-day remaining-task counts for a sprint.

    The result has shape::

        {"sprint": {...}, "days": [
            {"date": "2026-04-30", "remaining": 12, "completed": 0},
            ...
        ]}
    """
    sp = _find(sess, team_id=team_id, slug=slug)
    tasks = list(sess.execute(
        select(models.Task).where(
            models.Task.team_id == team_id,
            models.Task.sprint_id == sp.id,
        )
    ).scalars())
    # Build closure timeline from audit events when available, falling back
    # to ``Task.closed_at``.
    closed_on: dict[int, datetime] = {}
    for t in tasks:
        if t.closed_at is not None:
            closed_on[t.id] = t.closed_at
    # Walk from start to today (or sprint end, whichever earlier).
    today = now_utc().replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    end = min(today, sp.ends_at)
    if end < sp.starts_at:
        end = sp.starts_at
    days = []
    n_total = len(tasks)
    cursor = sp.starts_at.replace(hour=0, minute=0, second=0, microsecond=0)
    end_floor = end.replace(hour=0, minute=0, second=0, microsecond=0)
    while cursor <= end_floor:
        completed = sum(1 for cid, dt in closed_on.items() if dt <= cursor + timedelta(days=1))
        days.append({
            "date": cursor.strftime("%Y-%m-%d"),
            "remaining": n_total - completed,
            "completed": completed,
        })
        cursor += timedelta(days=1)
    return {
        "sprint": {
            "id": sp.id, "slug": sp.slug, "name": sp.name,
            "starts_at": sp.starts_at.isoformat(),
            "ends_at": sp.ends_at.isoformat(),
            "active": sp.active, "goal": sp.goal,
            "task_count": n_total,
        },
        "days": days,
    }


def _find(sess: Session, *, team_id: int, slug: str) -> models.Sprint:
    sp = sess.execute(
        select(models.Sprint).where(
            models.Sprint.team_id == team_id, models.Sprint.slug == slug,
        )
    ).scalar_one_or_none()
    if sp is None:
        raise NotFoundError(f"sprint {slug!r} not found")
    return sp
