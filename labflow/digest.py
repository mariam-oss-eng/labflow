"""Weekly digest + risk alert generation."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .time_utils import now_utc


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

    def to_html(self) -> str:
        """Render an email-friendly HTML digest (inline styles, no JS).

        Inlining styles is the only thing that survives the major email
        clients (Gmail in particular strips ``<style>`` blocks). We keep
        the markup tiny and table-free for legibility on mobile.
        """
        from html import escape as _e

        def _section(title: str, items: list[str]) -> str:
            if not items:
                return ""
            lis = "".join(f"<li style='margin:.2em 0'>{x}</li>" for x in items)
            return (
                f"<h2 style='font-size:16px;color:#0f172a;margin:1.4em 0 .4em'>"
                f"{_e(title)}</h2>"
                f"<ul style='padding-left:1.2em;color:#1e293b;font-size:14px'>"
                f"{lis}</ul>"
            )

        head = (
            f"<div style='font-family:-apple-system,Segoe UI,Roboto,"
            f"sans-serif;max-width:640px;margin:0 auto;padding:24px;"
            f"color:#0f172a'>"
            f"<h1 style='font-size:20px;margin:0 0 .25em'>"
            f"🧪 LabFlow Weekly Digest</h1>"
            f"<div style='color:#64748b;font-size:13px;margin-bottom:1em'>"
            f"{self.period_start.date().isoformat()} → "
            f"{self.period_end.date().isoformat()}</div>"
            f"<div style='display:flex;gap:18px;flex-wrap:wrap;"
            f"font-size:13px;color:#334155;background:#f1f5f9;"
            f"padding:12px 16px;border-radius:10px'>"
            f"<div><strong>{len(self.meetings)}</strong> meetings</div>"
            f"<div><strong>{len(self.new_decisions)}</strong> decisions</div>"
            f"<div><strong>{len(self.closed_tasks)}</strong> closed</div>"
            f"<div><strong>{len(self.overdue_tasks)}</strong> overdue</div>"
            f"</div>"
        )
        body = "".join([
            _section("Decisions", [_e(d.statement) for d in self.new_decisions]),
            _section("Tasks closed", [f"✅ {_e(t.title)}" for t in self.closed_tasks]),
            _section(
                "⚠️ Overdue",
                [
                    f"@{_e(t.owner.handle if t.owner else 'unassigned')}: {_e(t.title)}"
                    + (f" <span style='color:#94a3b8'>(due "
                       f"{t.due_date.date().isoformat()})</span>"
                       if t.due_date else "")
                    for t in self.overdue_tasks
                ],
            ),
            _section(
                "🔎 Needs review",
                [f"{_e(t.title)} <span style='color:#94a3b8'>"
                 f"(uncertainty {t.uncertainty:.2f})</span>"
                 for t in self.high_uncertainty_tasks],
            ),
            _section("🚧 Blockers", [_e(b.description) for b in self.open_blockers]),
            _section("🧪 High-risk assumptions",
                     [_e(a.statement) for a in self.high_risk_assumptions]),
        ])
        foot = (
            f"<hr style='border:none;border-top:1px solid #e2e8f0;"
            f"margin:2em 0 1em'>"
            f"<div style='font-size:12px;color:#94a3b8'>"
            f"Sent by LabFlow. Manage notifications in your dashboard.</div>"
            f"</div>"
        )
        return head + body + foot


def build_weekly_digest(
    sess: Session, now: datetime | None = None, *, team_id: Optional[int] = None
) -> WeeklyDigest:
    """Build the weekly digest, optionally scoped to a single team."""
    now = now or now_utc()
    # Strip tz for SQLite compatibility — DB stores naive UTC datetimes.
    cmp_now = now.replace(tzinfo=None) if now.tzinfo is not None else now
    start = cmp_now - timedelta(days=7)

    def _scope(stmt):
        return stmt.where(models.Meeting.team_id == team_id) if team_id is not None else stmt

    meetings = list(
        sess.execute(
            _scope(select(models.Meeting).where(models.Meeting.occurred_at >= start))
        ).scalars()
    )
    decisions_q = select(models.Decision).where(models.Decision.created_at >= start)
    if team_id is not None:
        decisions_q = decisions_q.where(models.Decision.team_id == team_id)
    decisions = list(sess.execute(decisions_q).scalars())

    closed_q = select(models.Task).where(
        models.Task.closed_at.is_not(None), models.Task.closed_at >= start
    )
    if team_id is not None:
        closed_q = closed_q.where(models.Task.team_id == team_id)
    closed = list(sess.execute(closed_q).scalars())

    overdue_q = select(models.Task).where(
        models.Task.status != "done",
        models.Task.due_date.is_not(None),
        models.Task.due_date < cmp_now,
    )
    if team_id is not None:
        overdue_q = overdue_q.where(models.Task.team_id == team_id)
    overdue = list(sess.execute(overdue_q).scalars())

    high_unc_q = select(models.Task).where(
        models.Task.status != "done",
        models.Task.uncertainty >= 0.5,
    )
    if team_id is not None:
        high_unc_q = high_unc_q.where(models.Task.team_id == team_id)
    high_unc = list(sess.execute(high_unc_q).scalars())

    blockers_q = select(models.Blocker).where(models.Blocker.resolved.is_(False))
    if team_id is not None:
        blockers_q = blockers_q.where(models.Blocker.team_id == team_id)
    blockers = list(sess.execute(blockers_q).scalars())

    assumptions_q = select(models.Assumption).where(
        models.Assumption.risk == "high",
        models.Assumption.validated.is_(False),
    )
    if team_id is not None:
        assumptions_q = assumptions_q.where(models.Assumption.team_id == team_id)
    assumptions = list(sess.execute(assumptions_q).scalars())

    return WeeklyDigest(
        period_start=start,
        period_end=cmp_now,
        meetings=meetings,
        new_decisions=decisions,
        closed_tasks=closed,
        overdue_tasks=overdue,
        high_uncertainty_tasks=high_unc,
        open_blockers=blockers,
        high_risk_assumptions=assumptions,
    )
