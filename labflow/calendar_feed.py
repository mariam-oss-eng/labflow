"""iCalendar feed generator (v0.6).

Renders a team's open tasks with due dates as a standards-compliant
``text/calendar`` (RFC 5545) feed. Subscribe with any calendar app —
Google, Apple, Outlook all accept the same URL.

We deliberately hand-roll the output instead of pulling in ``icalendar``
because the spec is small and adding a 60 KB transitive dep for one
endpoint is not worth it.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .time_utils import now_utc

PRODID = "-//LabFlow//Tasks//EN"


def _fmt(dt: datetime) -> str:
    """Format a naive UTC datetime as iCalendar UTC timestamp (Zulu)."""
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _escape(text: str) -> str:
    """Escape commas, semicolons, backslashes, and CRLF per RFC 5545 §3.3.11."""
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace("\n", "\\n")
        .replace("\r", "")
    )


def _fold(line: str) -> str:
    """Fold long lines per RFC 5545 §3.1 (max 75 octets per line)."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    pieces = []
    cur = b""
    for ch in encoded:
        cur += bytes([ch])
        if len(cur) >= 73:
            pieces.append(cur.decode("utf-8", errors="ignore"))
            cur = b""
    if cur:
        pieces.append(cur.decode("utf-8", errors="ignore"))
    return "\r\n ".join(pieces)


def render_team_calendar(sess: Session, *, team_id: int) -> str:
    tasks = list(
        sess.execute(
            select(models.Task)
            .where(models.Task.team_id == team_id,
                   models.Task.due_date.is_not(None),
                   models.Task.status != "done")
            .order_by(models.Task.due_date)
        ).scalars()
    )
    now = now_utc().replace(tzinfo=None)
    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:LabFlow tasks (team {team_id})",
        "X-PUBLISHED-TTL:PT1H",
    ]
    for t in tasks:
        owner = t.owner.handle if t.owner is not None else "unassigned"
        due = t.due_date
        # Treat the due date as a 30-min event so calendar UIs render it.
        end = (due if due is not None else now) + timedelta(minutes=30)
        summary = f"[LabFlow] {t.title}"
        desc_parts = [
            f"Status: {t.status}",
            f"Owner: @{owner}",
            f"Confidence: {t.confidence:.2f}",
        ]
        if t.description:
            desc_parts.append("")
            desc_parts.append(t.description)
        body = [
            "BEGIN:VEVENT",
            _fold(f"UID:labflow-task-{t.id}@labflow"),
            f"DTSTAMP:{_fmt(now)}",
            f"DTSTART:{_fmt(due)}",
            f"DTEND:{_fmt(end)}",
            _fold(f"SUMMARY:{_escape(summary)}"),
            _fold(f"DESCRIPTION:{_escape(chr(10).join(desc_parts))}"),
            f"STATUS:{'CONFIRMED' if t.confidence >= 0.5 else 'TENTATIVE'}",
            "END:VEVENT",
        ]
        lines.extend(body)
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
