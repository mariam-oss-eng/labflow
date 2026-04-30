"""Time-travel queries (v0.9).

Reconstruct the historical state of a meeting/task/decision by replaying the
``audit_events`` log up to a given timestamp. Useful for "what did this task
look like before it was closed?" or "show me the decision graph as of last
Friday".

Implementation
--------------
We treat the current row in the table as the *latest* state, then "rewind"
fields whose audit history we recorded. The audit log stores semantic
actions (``task.transition``, ``meeting.finalized``, etc.); for each action
we know which fields it touches and what the previous values were (recorded
in the ``metadata_json`` blob). Replaying the events newer than ``as_of``
*in reverse* gives the historical snapshot.

Limitations:
  * Only fields explicitly recorded in audit metadata can be rewound. For
    others, the returned snapshot is the current value (callers are warned
    via the ``incomplete: true`` field).
  * The transcript itself is immutable post-finalize, so meetings older than
    finalization always show the canonical text.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .config import get_settings
from .errors import NotFoundError, ValidationError


def _validate_as_of(as_of: datetime) -> datetime:
    settings = get_settings()
    now = datetime.utcnow()
    max_age_days = settings.timetravel_max_days
    if as_of > now:
        raise ValidationError("as_of cannot be in the future")
    if (now - as_of).days > max_age_days:
        raise ValidationError(f"as_of beyond max lookback ({max_age_days} days)")
    return as_of


def _events_after(sess: Session, *, team_id: int, entity_type: str,
                  entity_id: int, as_of: datetime) -> list[models.AuditEvent]:
    """Newer-first audit events for an entity strictly after ``as_of``."""
    return list(sess.execute(
        select(models.AuditEvent).where(
            models.AuditEvent.team_id == team_id,
            models.AuditEvent.entity_type == entity_type,
            models.AuditEvent.entity_id == entity_id,
            models.AuditEvent.created_at > as_of,
        ).order_by(models.AuditEvent.created_at.desc())
    ).scalars())


def task_as_of(sess: Session, *, team_id: int, task_id: int,
               as_of: datetime) -> dict:
    """Return the state of a task as of ``as_of``.

    Walks backwards through ``task.transition`` events to reconstruct the
    historical ``state``. Other fields fall back to the current row.
    """
    _validate_as_of(as_of)
    task = sess.get(models.Task, task_id)
    if task is None or task.team_id != team_id:
        raise NotFoundError("task not found")
    if task.created_at and task.created_at > as_of:
        raise NotFoundError("task did not exist at as_of")
    events = _events_after(sess, team_id=team_id, entity_type="task",
                           entity_id=task_id, as_of=as_of)
    state = task.state
    status = task.status
    closed_at = task.closed_at
    incomplete = False
    for evt in events:
        if evt.action == "task.transition" and evt.metadata_json:
            try:
                meta = json.loads(evt.metadata_json)
            except Exception:  # noqa: BLE001
                incomplete = True
                continue
            state = meta.get("from", state)
            # Heuristic: if we rewound past a closure, undo close fields.
            if status == "closed":
                status = "open"
                closed_at = None
        elif evt.action == "task.created":
            # Task didn't exist before this point.
            raise NotFoundError("task did not exist at as_of")
        else:
            incomplete = True  # we don't know how to rewind this action
    return {
        "id": task.id, "title": task.title, "status": status,
        "state": state, "kind": task.kind,
        "owner_id": task.owner_id,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "closed_at": closed_at.isoformat() if closed_at else None,
        "as_of": as_of.isoformat(),
        "incomplete": incomplete,
    }


def meeting_as_of(sess: Session, *, team_id: int, meeting_id: int,
                  as_of: datetime) -> dict:
    """Return a meeting snapshot as of ``as_of`` (decisions/tasks at that time)."""
    _validate_as_of(as_of)
    m = sess.get(models.Meeting, meeting_id)
    if m is None or m.team_id != team_id:
        raise NotFoundError("meeting not found")
    if m.occurred_at > as_of and (m.created_at and m.created_at > as_of):
        raise NotFoundError("meeting did not exist at as_of")
    decisions = [
        {"id": d.id, "statement": d.statement,
         "rationale": d.rationale,
         "created_at": d.created_at.isoformat() if d.created_at else None}
        for d in m.decisions
        if d.created_at is None or d.created_at <= as_of
    ]
    tasks_snapshot = []
    for t in m.tasks:
        if t.created_at and t.created_at > as_of:
            continue
        try:
            tasks_snapshot.append(
                task_as_of(sess, team_id=team_id, task_id=t.id, as_of=as_of)
            )
        except NotFoundError:
            continue
    return {
        "id": m.id, "title": m.title,
        "occurred_at": m.occurred_at.isoformat(),
        "as_of": as_of.isoformat(),
        "decisions": decisions,
        "tasks": tasks_snapshot,
    }
