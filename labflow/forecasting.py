"""Forecasting helpers (v0.10).

Two estimators, both pure-Python and dependency-free:

* :func:`sprint_forecast` — least-squares linear regression on the
  burndown's ``remaining`` series, projecting forward to find when the
  line crosses zero. Returns a confidence band proportional to the
  residual variance.
* :func:`task_eta` — point estimate for a single task using the
  owner's median historical cycle time, with a global fallback.

These are deliberately simple — no scipy, no numpy. The whole point is
to give operators a directional signal that updates in real time as
their sprint progresses, not to outperform a planning expert.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models, sprints as sprints_mod
from .errors import NotFoundError
from .time_utils import now_utc


def _linear_regression(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Return (slope, intercept, residual_std) by least squares.

    Falls back to (0, mean(y), 0) when xs has insufficient variance.
    """
    n = len(xs)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0, 0.0
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x == 0:
        return 0.0, mean_y, 0.0
    cov_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = cov_xy / var_x
    intercept = mean_y - slope * mean_x
    if n > 2:
        residuals = [y - (slope * x + intercept) for x, y in zip(xs, ys)]
        # Sample std-dev of residuals (Bessel's correction).
        var = sum(r * r for r in residuals) / (n - 2)
        residual_std = math.sqrt(max(var, 0.0))
    else:
        residual_std = 0.0
    return slope, intercept, residual_std


def sprint_forecast(sess: Session, *, team_id: int, slug: str) -> dict:
    """Project the remaining-task line to zero and report ETA + confidence.

    Returns::

        {
          "sprint": {...},
          "method": "linear_regression",
          "samples": int,
          "slope_per_day": float,        # negative = burning down
          "remaining_today": float,
          "eta_iso": str | None,         # date when remaining ~ 0, or None
          "eta_within_sprint": bool | None,
          "confidence_days": float,      # half-width of ±1σ band
          "warning": str | None,
        }
    """
    bd = sprints_mod.burndown(sess, team_id=team_id, slug=slug)
    sprint_meta = bd["sprint"]
    days = bd["days"]
    if not days:
        return {
            "sprint": sprint_meta, "method": "linear_regression",
            "samples": 0, "slope_per_day": 0.0,
            "remaining_today": float(sprint_meta.get("task_count", 0)),
            "eta_iso": None, "eta_within_sprint": None,
            "confidence_days": 0.0,
            "warning": "no burndown samples",
        }

    xs = [float(i) for i in range(len(days))]
    ys = [float(d["remaining"]) for d in days]
    slope, intercept, residual_std = _linear_regression(xs, ys)
    remaining_today = ys[-1]
    warning: str | None = None

    if slope >= 0 or remaining_today <= 0:
        # Either flat/upward line — no ETA — or already burned down.
        eta_iso = None
        eta_within_sprint: bool | None = (None if remaining_today > 0
                                          else True)
        if slope >= 0 and remaining_today > 0:
            warning = ("burndown is flat or rising; cannot project "
                       "completion")
    else:
        # Solve slope*x + intercept = 0 → x* = -intercept/slope (in days
        # since sprint start). Anchor day 0 to the first sample's date.
        x_star = -intercept / slope
        first_date = datetime.strptime(days[0]["date"], "%Y-%m-%d")
        eta_dt = first_date + timedelta(days=max(x_star, 0.0))
        eta_iso = eta_dt.date().isoformat()
        end_dt = datetime.fromisoformat(sprint_meta["ends_at"])
        eta_within_sprint = eta_dt <= end_dt

    # Confidence: how many days of error at the ±1σ level. With slope=0
    # we can't translate residual into days, so report 0.
    confidence_days = (residual_std / abs(slope)) if slope < 0 else 0.0

    return {
        "sprint": sprint_meta,
        "method": "linear_regression",
        "samples": len(days),
        "slope_per_day": round(slope, 4),
        "remaining_today": remaining_today,
        "eta_iso": eta_iso,
        "eta_within_sprint": eta_within_sprint,
        "confidence_days": round(confidence_days, 2),
        "warning": warning,
    }


def _owner_cycle_times(sess: Session, *, team_id: int, owner_id: int | None) -> list[float]:
    """Return historical cycle-time deltas in *hours* for closed tasks."""
    q = select(models.Task).where(
        models.Task.team_id == team_id,
        models.Task.closed_at.is_not(None),
        models.Task.created_at.is_not(None),
    )
    if owner_id is not None:
        q = q.where(models.Task.owner_id == owner_id)
    rows = sess.execute(q).scalars().all()
    out: list[float] = []
    for t in rows:
        delta = (t.closed_at - t.created_at).total_seconds() / 3600.0
        if delta > 0:
            out.append(delta)
    return out


def task_eta(sess: Session, *, team_id: int, task_id: int) -> dict:
    """Per-task ETA using owner cycle-time stats (or team-wide fallback).

    Returns::

        {
          "task_id": int,
          "owner_id": int | None,
          "method": "median_cycle_time" | "team_fallback" | "no_history",
          "samples": int,
          "median_hours": float | None,
          "eta_iso": str | None,
          "already_closed": bool,
        }
    """
    task = sess.get(models.Task, task_id)
    if task is None or task.team_id != team_id:
        raise NotFoundError(f"task {task_id} not found")
    if task.closed_at is not None:
        return {
            "task_id": task.id, "owner_id": task.owner_id,
            "method": "already_closed", "samples": 0,
            "median_hours": None, "eta_iso": task.closed_at.isoformat(),
            "already_closed": True,
        }

    samples = _owner_cycle_times(sess, team_id=team_id, owner_id=task.owner_id)
    method = "median_cycle_time"
    if len(samples) < 3:
        # Not enough owner data — fall back to team-wide.
        samples = _owner_cycle_times(sess, team_id=team_id, owner_id=None)
        method = "team_fallback"
    if not samples:
        return {
            "task_id": task.id, "owner_id": task.owner_id,
            "method": "no_history", "samples": 0,
            "median_hours": None, "eta_iso": None,
            "already_closed": False,
        }
    median_hours = statistics.median(samples)
    eta_dt = (task.created_at or now_utc()) + timedelta(hours=median_hours)
    return {
        "task_id": task.id, "owner_id": task.owner_id,
        "method": method, "samples": len(samples),
        "median_hours": round(median_hours, 2),
        "eta_iso": eta_dt.isoformat(),
        "already_closed": False,
    }
