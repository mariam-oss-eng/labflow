"""Weekly digest + risk alert generation."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models


@dataclass
class WeeklyDigest:
    period_start: datetime
    period_end: datetime
    meetings: List[models.Meeting] = field(default_factory=list)
    new_decisions: List[models.Decision] = field(default_factory=list)
    closed_tasks: List[models.Task] = field(default_factory=list)
    overdue_tasks: List[models.Task] = field(default_factory=list)
    high_uncertainty_tasks: List[models.Task] = field(default_factory=list)
    open_blockers: List[models.Blocker] = field(default_factory=list)
    high_risk_assumptions: List[models.Assumption] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            f"# LabFlow Weekly Digest",
            f"_{self.period_start.date().isoformat()} → {self.period_end.date().isoformat()}_",
            "",
            f"**Meetings:** {len(self.meetings)}  "
            f"**New decisions:** {len(self.new_decisions)}  "
            f"**Tasks closed:** {len(self.closed_tasks)}  "
            f"**Overdue:** {len(self.overdue_tasks)}",
            "",
        ]
        if self.new_decisions:
            lines.append("## Decisions")
            for d in self.new_decisions:
                lines.append(f"- {d.statement}")
            lines.append("")
        if self.closed_tasks:
            lines.append("## Tasks closed this week")
            for t in self.closed_tasks:
                lines.append(f"- ✅ {t.title}")
            lines.append("")
        if self.overdue_tasks:
            lines.append("## ⚠️ Overdue")
            for t in self.overdue_tasks:
                due = t.due_date.date().isoformat() if t.due_date else "no date"
                owner = t.owner.handle if t.owner else "unassigned"
                lines.append(f"- @{owner}: {t.title} (due {due})")
            lines.append("")
        if self.high_uncertainty_tasks:
            lines.append("## 🔎 Needs review (high uncertainty)")
            for t in self.high_uncertainty_tasks:
                lines.append(f"- {t.title}  _(uncertainty {t.uncertainty:.2f})_")
            lines.append("")
        if self.open_blockers:
            lines.append("## 🚧 Open blockers")
            for b in self.open_blockers:
                lines.append(f"- {b.description}")
            lines.append("")
        if self.high_risk_assumptions:
            lines.append("## 🧪 High-risk assumptions")
            for a in self.high_risk_assumptions:
                lines.append(f"- {a.statement}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def build_weekly_digest(sess: Session, now: datetime | None = None) -> WeeklyDigest:
    now = now or datetime.utcnow()
    start = now - timedelta(days=7)

    meetings = list(
        sess.execute(
            select(models.Meeting).where(models.Meeting.occurred_at >= start)
        ).scalars()
    )
    decisions = list(
        sess.execute(
            select(models.Decision).where(models.Decision.created_at >= start)
        ).scalars()
    )
    closed = list(
        sess.execute(
            select(models.Task).where(
                models.Task.closed_at.is_not(None), models.Task.closed_at >= start
            )
        ).scalars()
    )
    overdue = list(
        sess.execute(
            select(models.Task).where(
                models.Task.status != "done",
                models.Task.due_date.is_not(None),
                models.Task.due_date < now,
            )
        ).scalars()
    )
    high_unc = list(
        sess.execute(
            select(models.Task).where(
                models.Task.status != "done",
                models.Task.uncertainty >= 0.5,
            )
        ).scalars()
    )
    blockers = list(
        sess.execute(
            select(models.Blocker).where(models.Blocker.resolved.is_(False))
        ).scalars()
    )
    assumptions = list(
        sess.execute(
            select(models.Assumption).where(
                models.Assumption.risk == "high",
                models.Assumption.validated.is_(False),
            )
        ).scalars()
    )

    return WeeklyDigest(
        period_start=start,
        period_end=now,
        meetings=meetings,
        new_decisions=decisions,
        closed_tasks=closed,
        overdue_tasks=overdue,
        high_uncertainty_tasks=high_unc,
        open_blockers=blockers,
        high_risk_assumptions=assumptions,
    )
