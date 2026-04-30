"""CSV exports for tasks/decisions (v0.8).

Hand-rolled to use the stdlib ``csv`` module so we don't pull in pandas for
something this small. Output is RFC 4180 compliant and includes a UTF-8
BOM so Excel auto-detects the encoding correctly.
"""
from __future__ import annotations

import csv
import io
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models


_TASK_COLUMNS = (
    "id", "title", "status", "state", "owner", "kind", "due_date",
    "created_at", "closed_at", "confidence", "uncertainty",
    "sprint", "workflow",
)
_DECISION_COLUMNS = (
    "id", "statement", "rationale", "confidence", "meeting_id", "created_at",
    "superseded_by_id",
)


def _csv(rows: Iterable[dict], columns: tuple[str, ...]) -> str:
    buf = io.StringIO()
    buf.write("\ufeff")  # UTF-8 BOM for Excel compatibility
    writer = csv.DictWriter(buf, fieldnames=columns,
                            quoting=csv.QUOTE_MINIMAL,
                            lineterminator="\r\n")  # RFC 4180
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in columns})
    return buf.getvalue()


def tasks_csv(sess: Session, *, team_id: int) -> str:
    """Render all tasks for a team as RFC 4180 CSV (with BOM)."""
    rows = []
    for t in sess.execute(
        select(models.Task).where(models.Task.team_id == team_id)
        .order_by(models.Task.created_at)
    ).scalars():
        owner_handle = ""
        if t.owner_id is not None:
            o = sess.get(models.Owner, t.owner_id)
            if o is not None:
                owner_handle = o.handle
        sprint_slug = ""
        if t.sprint_id is not None:
            sp = sess.get(models.Sprint, t.sprint_id)
            if sp is not None:
                sprint_slug = sp.slug
        wf_name = ""
        if t.workflow_id is not None:
            wf = sess.get(models.Workflow, t.workflow_id)
            if wf is not None:
                wf_name = wf.name
        rows.append({
            "id": t.id, "title": t.title, "status": t.status,
            "state": t.state or "", "owner": owner_handle, "kind": t.kind,
            "due_date": t.due_date.isoformat() if t.due_date else "",
            "created_at": t.created_at.isoformat() if t.created_at else "",
            "closed_at": t.closed_at.isoformat() if t.closed_at else "",
            "confidence": round(t.confidence, 3),
            "uncertainty": round(t.uncertainty, 3),
            "sprint": sprint_slug, "workflow": wf_name,
        })
    return _csv(rows, _TASK_COLUMNS)


def decisions_csv(sess: Session, *, team_id: int) -> str:
    rows = []
    for d in sess.execute(
        select(models.Decision).where(models.Decision.team_id == team_id)
        .order_by(models.Decision.created_at)
    ).scalars():
        rows.append({
            "id": d.id, "statement": d.statement,
            "rationale": d.rationale or "",
            "confidence": round(d.confidence, 3),
            "meeting_id": d.meeting_id,
            "created_at": d.created_at.isoformat() if d.created_at else "",
            "superseded_by_id": d.superseded_by_id or "",
        })
    return _csv(rows, _DECISION_COLUMNS)
