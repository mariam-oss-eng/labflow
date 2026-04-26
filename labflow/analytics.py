"""Team analytics (v0.6).

Computes the operational metrics LabFlow's typed graph makes possible
*for free* — without an external BI tool:

* **cycle_time_p50_days** — median wall time between task creation and
  completion for tasks closed in the window.
* **throughput** — count of tasks closed in the window.
* **completion_rate** — closed / (opened in window) ratio (0..1).
* **blocker_rate** — open blockers per finalized meeting.
* **top_owners** — handles ranked by closed-task count in the window.
* **weekly_trend** — list of `{week_start, opened, closed}` for the last 8
  ISO weeks (handy as a sparkline on the dashboard).

All queries are team-scoped and run in a single SQL pass each (no Python
post-processing on million-row sets), so the endpoint stays fast even
with a heavy backlog.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import List

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models
from .time_utils import now_utc


@dataclass
class WeeklyPoint:
    week_start: str
    opened: int
    closed: int


@dataclass
class Analytics:
    period_start: str
    period_end: str
    meetings: int
    finalized_meetings: int
    decisions: int
    tasks_opened: int
    tasks_closed: int
    cycle_time_p50_days: float | None
    cycle_time_p90_days: float | None
    completion_rate: float
    blocker_rate: float
    top_owners: List[dict] = field(default_factory=list)
    weekly_trend: List[WeeklyPoint] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["weekly_trend"] = [asdict(w) for w in self.weekly_trend]
        return d


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    rank = (pct / 100.0) * (len(s) - 1)
    lo, hi = math.floor(rank), math.ceil(rank)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (rank - lo)


def _iso_week_start(d: datetime) -> datetime:
    """Return the Monday 00:00 of the ISO week containing ``d`` (naive UTC)."""
    monday = d - timedelta(days=d.weekday())
    return datetime(monday.year, monday.month, monday.day)


def compute(
    sess: Session, *, team_id: int, days: int = 30, now: datetime | None = None,
) -> Analytics:
    now = (now or now_utc()).replace(tzinfo=None)
    start = now - timedelta(days=max(1, days))

    meetings_total = sess.execute(
        select(func.count())
        .select_from(models.Meeting)
        .where(models.Meeting.team_id == team_id,
               models.Meeting.occurred_at >= start)
    ).scalar_one()
    finalized = sess.execute(
        select(func.count())
        .select_from(models.Meeting)
        .where(models.Meeting.team_id == team_id,
               models.Meeting.finalized.is_(True),
               models.Meeting.occurred_at >= start)
    ).scalar_one()
    decisions = sess.execute(
        select(func.count())
        .select_from(models.Decision)
        .where(models.Decision.team_id == team_id,
               models.Decision.created_at >= start)
    ).scalar_one()
    opened = sess.execute(
        select(func.count())
        .select_from(models.Task)
        .where(models.Task.team_id == team_id,
               models.Task.created_at >= start)
    ).scalar_one()
    closed_rows = list(
        sess.execute(
            select(models.Task.created_at, models.Task.closed_at)
            .where(models.Task.team_id == team_id,
                   models.Task.closed_at.is_not(None),
                   models.Task.closed_at >= start)
        ).all()
    )
    closed = len(closed_rows)
    cycle_days = [
        (cl - cr).total_seconds() / 86400.0
        for cr, cl in closed_rows if cr and cl and cl >= cr
    ]
    cycle_p50 = _percentile(cycle_days, 50)
    cycle_p90 = _percentile(cycle_days, 90)

    open_blockers = sess.execute(
        select(func.count())
        .select_from(models.Blocker)
        .where(models.Blocker.team_id == team_id,
               models.Blocker.resolved.is_(False))
    ).scalar_one()
    blocker_rate = (open_blockers / finalized) if finalized else 0.0
    completion_rate = (closed / opened) if opened else 0.0

    # Top owners by closed-task count in the window.
    owner_rows = sess.execute(
        select(models.Owner.handle, func.count(models.Task.id).label("n"))
        .join(models.Task, models.Task.owner_id == models.Owner.id)
        .where(models.Task.team_id == team_id,
               models.Task.closed_at.is_not(None),
               models.Task.closed_at >= start)
        .group_by(models.Owner.handle)
        .order_by(func.count(models.Task.id).desc())
        .limit(5)
    ).all()
    top_owners = [{"handle": h, "closed": int(n)} for h, n in owner_rows]

    # Weekly trend — last 8 ISO weeks.
    trend: list[WeeklyPoint] = []
    for w in range(7, -1, -1):
        week_end = _iso_week_start(now) - timedelta(weeks=w - 1)
        week_start = week_end - timedelta(weeks=1)
        wk_opened = sess.execute(
            select(func.count())
            .select_from(models.Task)
            .where(models.Task.team_id == team_id,
                   models.Task.created_at >= week_start,
                   models.Task.created_at < week_end)
        ).scalar_one()
        wk_closed = sess.execute(
            select(func.count())
            .select_from(models.Task)
            .where(models.Task.team_id == team_id,
                   models.Task.closed_at >= week_start,
                   models.Task.closed_at < week_end)
        ).scalar_one()
        trend.append(WeeklyPoint(
            week_start=week_start.date().isoformat(),
            opened=int(wk_opened), closed=int(wk_closed),
        ))

    return Analytics(
        period_start=start.isoformat(),
        period_end=now.isoformat(),
        meetings=int(meetings_total),
        finalized_meetings=int(finalized),
        decisions=int(decisions),
        tasks_opened=int(opened),
        tasks_closed=int(closed),
        cycle_time_p50_days=round(cycle_p50, 2) if cycle_p50 is not None else None,
        cycle_time_p90_days=round(cycle_p90, 2) if cycle_p90 is not None else None,
        completion_rate=round(completion_rate, 4),
        blocker_rate=round(blocker_rate, 4),
        top_owners=top_owners,
        weekly_trend=trend,
    )
