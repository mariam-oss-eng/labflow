"""Time tracking on tasks (v0.14).

Two flavours of entry share one table:

* **Timer** — start a timer on a task; later, stop it (or stop all open
  timers for the same owner before starting a new one — *"only one
  running timer per owner"*).
* **Manual** — log a closed entry with explicit ``started_at`` and
  ``ended_at``.

All write helpers append :func:`labflow.audit.record` events so
time edits are part of the v0.10 hash chain.

The aggregator (:func:`task_summary`) returns total seconds tracked plus
a breakdown by owner — used both by the REST endpoint and the upcoming
``Task.effort_hours`` reconciliation.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit as audit_mod, models
from .errors import ConflictError, NotFoundError, ValidationError
from .time_utils import now_utc


def _strip(d: datetime) -> datetime:
    return d.replace(tzinfo=None) if d.tzinfo else d


def _open_timer_for(
    sess: Session, *, team_id: int, owner_id: Optional[int],
) -> Optional[models.TimeEntry]:
    """Return the open timer for ``owner_id`` in ``team_id``, if any.

    ``owner_id is None`` matches the team-level open timer (rare —
    usually you assign owners)."""
    q = select(models.TimeEntry).where(
        models.TimeEntry.team_id == team_id,
        models.TimeEntry.ended_at.is_(None),
        models.TimeEntry.source == "timer",
    )
    if owner_id is None:
        q = q.where(models.TimeEntry.owner_id.is_(None))
    else:
        q = q.where(models.TimeEntry.owner_id == owner_id)
    return sess.execute(q).scalars().first()


def start_timer(
    sess: Session, *, team_id: int, task_id: int,
    owner_id: Optional[int] = None, actor: str = "system",
    note: Optional[str] = None,
) -> models.TimeEntry:
    """Start a timer on ``task_id`` for ``owner_id``.

    If the owner already has an open timer it is *stopped first* (the
    new timer's ``started_at`` becomes the previous one's ``ended_at``)
    so the per-owner-single-timer invariant is preserved without
    silently dropping data.
    """
    task = sess.get(models.Task, task_id)
    if task is None or task.team_id != team_id:
        raise NotFoundError(f"task {task_id} not found")

    now = _strip(now_utc())
    open_t = _open_timer_for(sess, team_id=team_id, owner_id=owner_id)
    if open_t is not None:
        open_t.ended_at = now
        audit_mod.record(
            sess, team_id=team_id, action="time.timer_stopped_implicit",
            entity_type="time_entry", entity_id=open_t.id, actor=actor,
            metadata={"task_id": open_t.task_id},
        )
    entry = models.TimeEntry(
        team_id=team_id, task_id=task_id, owner_id=owner_id,
        started_at=now, ended_at=None, note=note, source="timer",
    )
    sess.add(entry)
    sess.flush()
    audit_mod.record(
        sess, team_id=team_id, action="time.timer_started",
        entity_type="time_entry", entity_id=entry.id, actor=actor,
        metadata={"task_id": task_id},
    )
    return entry


def stop_timer(
    sess: Session, *, team_id: int, owner_id: Optional[int] = None,
    actor: str = "system",
) -> models.TimeEntry:
    """Stop the currently-open timer for ``owner_id``."""
    open_t = _open_timer_for(sess, team_id=team_id, owner_id=owner_id)
    if open_t is None:
        raise ConflictError("no open timer for that owner")
    open_t.ended_at = _strip(now_utc())
    audit_mod.record(
        sess, team_id=team_id, action="time.timer_stopped",
        entity_type="time_entry", entity_id=open_t.id, actor=actor,
        metadata={"task_id": open_t.task_id,
                  "duration_seconds": int(
                      (open_t.ended_at - open_t.started_at).total_seconds()
                  )},
    )
    return open_t


def log_manual(
    sess: Session, *, team_id: int, task_id: int,
    started_at: datetime, ended_at: datetime,
    owner_id: Optional[int] = None, note: Optional[str] = None,
    actor: str = "system",
) -> models.TimeEntry:
    """Log a closed entry retrospectively."""
    task = sess.get(models.Task, task_id)
    if task is None or task.team_id != team_id:
        raise NotFoundError(f"task {task_id} not found")
    started_at = _strip(started_at)
    ended_at = _strip(ended_at)
    if ended_at <= started_at:
        raise ValidationError("ended_at must be after started_at")
    if (ended_at - started_at) > timedelta(hours=24):
        raise ValidationError("manual entries are capped at 24 hours")
    entry = models.TimeEntry(
        team_id=team_id, task_id=task_id, owner_id=owner_id,
        started_at=started_at, ended_at=ended_at,
        note=note, source="manual",
    )
    sess.add(entry)
    sess.flush()
    audit_mod.record(
        sess, team_id=team_id, action="time.logged",
        entity_type="time_entry", entity_id=entry.id, actor=actor,
        metadata={"task_id": task_id,
                  "duration_seconds": int(
                      (ended_at - started_at).total_seconds()
                  )},
    )
    return entry


def list_for_task(
    sess: Session, *, team_id: int, task_id: int,
) -> list[models.TimeEntry]:
    return list(sess.execute(
        select(models.TimeEntry)
        .where(models.TimeEntry.team_id == team_id,
               models.TimeEntry.task_id == task_id)
        .order_by(models.TimeEntry.started_at.desc())
    ).scalars().all())


def task_summary(
    sess: Session, *, team_id: int, task_id: int, now: Optional[datetime] = None,
) -> dict:
    """Return totals for one task. Open timers are charged up to ``now``."""
    cur = _strip(now or now_utc())
    rows = list_for_task(sess, team_id=team_id, task_id=task_id)
    total = 0
    by_owner: dict[Optional[int], int] = defaultdict(int)
    open_count = 0
    for r in rows:
        end = r.ended_at or cur
        if end <= r.started_at:
            continue
        secs = int((end - r.started_at).total_seconds())
        total += secs
        by_owner[r.owner_id] += secs
        if r.ended_at is None:
            open_count += 1
    return {
        "task_id": task_id,
        "total_seconds": total,
        "total_hours": round(total / 3600.0, 3),
        "open_timer_count": open_count,
        "by_owner": [
            {"owner_id": k, "seconds": v} for k, v in sorted(
                by_owner.items(), key=lambda kv: -kv[1]
            )
        ],
    }


def team_report(
    sess: Session, *, team_id: int, since: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Aggregate per-task and per-owner totals across the team."""
    cur = _strip(now or now_utc())
    q = select(models.TimeEntry).where(models.TimeEntry.team_id == team_id)
    if since is not None:
        q = q.where(models.TimeEntry.started_at >= _strip(since))
    rows = list(sess.execute(q).scalars().all())

    by_task: dict[int, int] = defaultdict(int)
    by_owner: dict[Optional[int], int] = defaultdict(int)
    for r in rows:
        end = r.ended_at or cur
        if end <= r.started_at:
            continue
        secs = int((end - r.started_at).total_seconds())
        by_task[r.task_id] += secs
        by_owner[r.owner_id] += secs
    return {
        "by_task": [
            {"task_id": k, "seconds": v} for k, v in sorted(
                by_task.items(), key=lambda kv: -kv[1]
            )
        ],
        "by_owner": [
            {"owner_id": k, "seconds": v} for k, v in sorted(
                by_owner.items(), key=lambda kv: -kv[1]
            )
        ],
        "entry_count": len(rows),
    }
