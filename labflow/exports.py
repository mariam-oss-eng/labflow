"""Markdown / JSON exports for a meeting's structured output."""
from __future__ import annotations

import json
from typing import Any, Dict

from . import models


def meeting_to_dict(meeting: models.Meeting) -> Dict[str, Any]:
    return {
        "meeting": {
            "id": meeting.id,
            "title": meeting.title,
            "type": meeting.meeting_type,
            "occurred_at": meeting.occurred_at.isoformat(),
            "finalized": meeting.finalized,
        },
        "decisions": [
            {"statement": d.statement, "rationale": d.rationale, "confidence": d.confidence}
            for d in meeting.decisions
        ],
        "tasks": [
            {
                "title": t.title,
                "description": t.description,
                "owner": t.owner.handle if t.owner else None,
                "due_date": t.due_date.isoformat() if t.due_date else None,
                "status": t.status,
                "kind": t.kind,
                "uncertainty": t.uncertainty,
                "depends_on": [dep.title for dep in t.depends_on],
            }
            for t in meeting.tasks
        ],
        "experiments": [
            {
                "name": e.name,
                "hypothesis": e.hypothesis,
                "method": e.method,
                "metrics": e.metrics.split(",") if e.metrics else [],
                "dataset": e.dataset,
                "owner": e.owner.handle if e.owner else None,
            }
            for e in meeting.experiments
        ],
        "assumptions": [
            {"statement": a.statement, "risk": a.risk, "validated": a.validated}
            for a in meeting.assumptions
        ],
        "blockers": [
            {"description": b.description, "resolved": b.resolved}
            for b in meeting.blockers
        ],
    }


def meeting_to_json(meeting: models.Meeting) -> str:
    return json.dumps(meeting_to_dict(meeting), indent=2, sort_keys=True)


def meeting_to_markdown(meeting: models.Meeting) -> str:
    out: list[str] = []
    out.append(f"# {meeting.title}")
    out.append(f"_{meeting.meeting_type} · {meeting.occurred_at.date().isoformat()}_")
    out.append("")
    if meeting.decisions:
        out.append("## Decisions")
        for d in meeting.decisions:
            line = f"- {d.statement}"
            if d.confidence < 0.7:
                line += f"  _(confidence {d.confidence:.2f})_"
            out.append(line)
        out.append("")
    if meeting.tasks:
        out.append("## Action items")
        for t in meeting.tasks:
            owner = f"@{t.owner.handle}" if t.owner else "_unassigned_"
            due = t.due_date.date().isoformat() if t.due_date else "no deadline"
            checkbox = "[x]" if t.status == "done" else "[ ]"
            out.append(f"- {checkbox} **{t.kind}** {owner} — {t.title} (due {due})")
            if t.depends_on:
                deps = ", ".join(d.title for d in t.depends_on)
                out.append(f"    - depends on: {deps}")
        out.append("")
    if meeting.experiments:
        out.append("## Experiments")
        for e in meeting.experiments:
            out.append(f"- **{e.name}**")
            if e.hypothesis:
                out.append(f"    - hypothesis: {e.hypothesis}")
            if e.metrics:
                out.append(f"    - metrics: {e.metrics}")
        out.append("")
    if meeting.assumptions:
        out.append("## Assumptions")
        for a in meeting.assumptions:
            out.append(f"- ({a.risk}) {a.statement}")
        out.append("")
    if meeting.blockers:
        out.append("## Blockers")
        for b in meeting.blockers:
            out.append(f"- {b.description}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"
