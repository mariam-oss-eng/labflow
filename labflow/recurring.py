"""Recurring tasks (v0.12).

Templates that materialise into normal :class:`labflow.models.Task` rows
on a cadence. The materialiser is a pure function — call it from a job
or from CRON — and is *idempotent* w.r.t. ``next_run_at``: once a
template fires, ``next_run_at`` is bumped to the next slot, so running
the sweeper twice in the same minute is safe.

No new dependencies. Day-of-month past the end of the month is clamped
to the last valid day (so the 31st in February becomes the 28th/29th).
"""
from __future__ import annotations

import calendar
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit as audit_mod, models
from .errors import NotFoundError, ValidationError
from .time_utils import now_utc

VALID_CADENCE = frozenset({"daily", "weekly", "monthly"})


# ---------------------------------------------------------------------------
# Cadence math
# ---------------------------------------------------------------------------
def _clamp_dom(year: int, month: int, dom: int) -> int:
    last = calendar.monthrange(year, month)[1]
    return min(max(dom, 1), last)


def next_run_after(
    *, after: datetime, cadence: str, interval: int = 1,
    day_of_week: int | None = None, day_of_month: int | None = None,
) -> datetime:
    """Compute the next firing instant *strictly after* ``after``.

    ``after`` is naive UTC. The result is also naive UTC at midnight
    UTC of the target date so jobs fire deterministically once a day.
    """
    if interval < 1:
        interval = 1
    base = after.replace(hour=0, minute=0, second=0, microsecond=0)

    if cadence == "daily":
        candidate = base + timedelta(days=interval)
    elif cadence == "weekly":
        if day_of_week is None or not 0 <= day_of_week <= 6:
            raise ValidationError("weekly cadence requires day_of_week 0..6")
        # ``after`` weekday — Python's Monday=0..Sunday=6 matches.
        days_ahead = (day_of_week - after.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7 * interval
        candidate = base + timedelta(days=days_ahead)
    elif cadence == "monthly":
        if day_of_month is None or not 1 <= day_of_month <= 31:
            raise ValidationError("monthly cadence requires day_of_month 1..31")
        # Try this month first, then walk forward by ``interval`` months.
        year, month = after.year, after.month
        dom = _clamp_dom(year, month, day_of_month)
        candidate = base.replace(day=1).replace(day=dom)
        if candidate <= after:
            month += interval
            year += (month - 1) // 12
            month = ((month - 1) % 12) + 1
            dom = _clamp_dom(year, month, day_of_month)
            candidate = datetime(year, month, dom)
    else:
        raise ValidationError(f"unsupported cadence {cadence!r}")
    return candidate


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def _validate(payload: dict[str, Any]) -> None:
    if payload.get("cadence") not in VALID_CADENCE:
        raise ValidationError(
            f"cadence must be one of {sorted(VALID_CADENCE)}"
        )
    if not (payload.get("template_title") or "").strip():
        raise ValidationError("template_title is required")
    interval = payload.get("interval", 1)
    if not isinstance(interval, int) or interval < 1:
        raise ValidationError("interval must be a positive integer")


def create_template(
    sess: Session, *, team_id: int, slug: str, payload: dict[str, Any],
    actor: str = "system",
) -> models.RecurringTask:
    _validate(payload)
    s = (slug or "").strip().lower()
    if not s:
        raise ValidationError("slug is required")
    existing = sess.execute(
        select(models.RecurringTask).where(
            models.RecurringTask.team_id == team_id,
            models.RecurringTask.slug == s,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ValidationError(f"recurring task {s!r} already exists")

    now = now_utc().replace(tzinfo=None)
    nxt = next_run_after(
        after=now, cadence=payload["cadence"],
        interval=int(payload.get("interval", 1)),
        day_of_week=payload.get("day_of_week"),
        day_of_month=payload.get("day_of_month"),
    )
    rt = models.RecurringTask(
        team_id=team_id,
        slug=s,
        cadence=payload["cadence"],
        interval=int(payload.get("interval", 1)),
        day_of_week=payload.get("day_of_week"),
        day_of_month=payload.get("day_of_month"),
        template_title=payload["template_title"].strip(),
        template_owner_id=payload.get("template_owner_id"),
        template_priority=payload.get("template_priority"),
        next_run_at=nxt,
        active=bool(payload.get("active", True)),
    )
    sess.add(rt)
    sess.flush()
    audit_mod.record(
        sess, team_id=team_id, action="recurring.created",
        entity_type="recurring_task", entity_id=rt.id, actor=actor,
        metadata={"slug": s, "cadence": rt.cadence},
    )
    return rt


def list_templates(sess: Session, *, team_id: int) -> list[models.RecurringTask]:
    return list(sess.execute(
        select(models.RecurringTask)
        .where(models.RecurringTask.team_id == team_id)
        .order_by(models.RecurringTask.slug.asc())
    ).scalars().all())


def delete_template(
    sess: Session, *, team_id: int, slug: str, actor: str = "system",
) -> None:
    rt = sess.execute(
        select(models.RecurringTask).where(
            models.RecurringTask.team_id == team_id,
            models.RecurringTask.slug == slug,
        )
    ).scalar_one_or_none()
    if rt is None:
        raise NotFoundError(f"recurring task {slug!r} not found")
    sess.delete(rt)
    audit_mod.record(
        sess, team_id=team_id, action="recurring.deleted",
        entity_type="recurring_task", entity_id=rt.id, actor=actor,
        metadata={"slug": slug},
    )


# ---------------------------------------------------------------------------
# Materialiser
# ---------------------------------------------------------------------------
def _get_or_create_recurring_meeting(
    sess: Session, *, team_id: int,
) -> models.Meeting:
    """Singleton container meeting that owns all materialised recurring tasks."""
    title = "Recurring tasks"
    m = sess.execute(
        select(models.Meeting).where(
            models.Meeting.team_id == team_id,
            models.Meeting.title == title,
            models.Meeting.meeting_type == "recurring",
        )
    ).scalar_one_or_none()
    if m is None:
        m = models.Meeting(
            team_id=team_id, title=title, meeting_type="recurring",
            transcript="", notes="",
        )
        sess.add(m)
        sess.flush()
    return m


def materialize_due(
    sess: Session, *, team_id: int | None = None, now: datetime | None = None,
) -> list[models.Task]:
    """Create ``Task`` rows for every active template whose ``next_run_at``
    is in the past, then advance ``next_run_at`` so re-runs are no-ops.

    Returns the list of newly created tasks (in creation order).
    """
    cur = (now or now_utc().replace(tzinfo=None))
    q = select(models.RecurringTask).where(
        models.RecurringTask.active.is_(True),
        models.RecurringTask.next_run_at <= cur,
    )
    if team_id is not None:
        q = q.where(models.RecurringTask.team_id == team_id)
    due = list(sess.execute(q).scalars().all())

    created: list[models.Task] = []
    # Cache per-team container meeting so a sweep of N templates doesn't
    # do N redundant meeting lookups.
    meeting_cache: dict[int, int] = {}
    for rt in due:
        if rt.team_id not in meeting_cache:
            m = _get_or_create_recurring_meeting(sess, team_id=rt.team_id)
            meeting_cache[rt.team_id] = m.id
        task = models.Task(
            team_id=rt.team_id,
            meeting_id=meeting_cache[rt.team_id],
            title=rt.template_title,
            owner_id=rt.template_owner_id,
            priority=rt.template_priority,
            status="open",
            state="todo",
            confidence=1.0,
            description=f"recurring:{rt.slug}",
        )
        sess.add(task)
        sess.flush()
        rt.last_run_at = cur
        rt.next_run_at = next_run_after(
            after=cur, cadence=rt.cadence, interval=rt.interval,
            day_of_week=rt.day_of_week, day_of_month=rt.day_of_month,
        )
        audit_mod.record(
            sess, team_id=rt.team_id, action="recurring.fired",
            entity_type="task", entity_id=task.id,
            metadata={"slug": rt.slug, "next_run_at": rt.next_run_at.isoformat()},
        )
        created.append(task)
    return created
