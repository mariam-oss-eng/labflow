"""Markdown bundle export (v0.13).

Compiles a team's full execution history into a single in-memory zip:

* ``manifest.json`` — top-level summary, version, generated-at,
  per-collection counts.
* ``audit.jsonl`` — one JSON object per audit row, in chain order.
* ``meetings/<id>-<slug>.md`` — meeting transcript + decisions + tasks.
* ``decisions/<id>.md``, ``tasks/<id>.md``, ``wiki/<slug>.md``.

Pure stdlib (``zipfile``, ``io``). No new runtime dependencies.

The bundle is *not* signed — pipe through the v0.10 backup signer if
you need integrity. The intent here is **portability** for archival,
demos, and human review.
"""
from __future__ import annotations

import io
import json
import zipfile
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import __version__, audit as audit_mod, models  # noqa: F401
from .time_utils import now_utc


def _esc(s: Any) -> str:
    if s is None:
        return ""
    return str(s)


def _slugify(s: str) -> str:
    out: list[str] = []
    for ch in (s or ""):
        if ch.isalnum():
            out.append(ch.lower())
        elif ch in (" ", "-", "_"):
            out.append("-")
    text = "".join(out).strip("-")
    return text or "untitled"


def _meeting_md(
    sess: Session, m: models.Meeting,
) -> str:
    decisions = list(sess.execute(
        select(models.Decision).where(models.Decision.meeting_id == m.id)
    ).scalars().all())
    tasks = list(sess.execute(
        select(models.Task).where(models.Task.meeting_id == m.id)
    ).scalars().all())
    parts: list[str] = [
        f"# {_esc(m.title)}",
        "",
        f"- **Meeting id:** `{m.id}`",
        f"- **Created:** {m.created_at.isoformat() if m.created_at else ''}",
        f"- **Type:** `{_esc(m.meeting_type)}`",
        f"- **Finalized:** `{bool(m.finalized)}`",
        "",
    ]
    if m.transcript:
        parts += ["## Transcript", "", "```", m.transcript, "```", ""]
    if decisions:
        parts += ["## Decisions", ""]
        for d in decisions:
            parts.append(f"- **#{d.id}** — {_esc(d.statement)} "
                         f"_(confidence={d.confidence:.2f})_")
        parts.append("")
    if tasks:
        parts += ["## Tasks", ""]
        for t in tasks:
            due = t.due_date.isoformat() if t.due_date else "—"
            parts.append(
                f"- `#{t.id}` **{_esc(t.title)}** — owner=#{_esc(t.owner_id)} "
                f"· status=`{_esc(t.status)}` · due={due}"
            )
        parts.append("")
    return "\n".join(parts)


def _decision_md(d: models.Decision) -> str:
    return "\n".join([
        f"# Decision #{d.id}",
        "",
        f"- **Meeting:** #{d.meeting_id}",
        f"- **Confidence:** {d.confidence:.2f}",
        "",
        "## Statement", "", _esc(d.statement), "",
        "## Rationale", "", _esc(d.rationale or ""), "",
    ])


def _task_md(t: models.Task) -> str:
    due = t.due_date.isoformat() if t.due_date else ""
    closed = t.closed_at.isoformat() if t.closed_at else ""
    return "\n".join([
        f"# Task #{t.id} — {_esc(t.title)}",
        "",
        f"- **Status:** `{_esc(t.status)}`",
        f"- **State:** `{_esc(t.state)}`",
        f"- **Priority:** `{_esc(t.priority)}`",
        f"- **Owner:** `#{_esc(t.owner_id)}`",
        f"- **Meeting:** #{_esc(t.meeting_id)}",
        f"- **Due:** {due}",
        f"- **Closed:** {closed}",
        "",
    ])


def _wiki_md(p: models.WikiPage) -> str:
    return "\n".join([
        f"# {_esc(p.title)}",
        "",
        f"<!-- slug={p.slug} updated_at={p.updated_at.isoformat()} -->",
        "",
        p.body or "",
        "",
    ])


def build_bundle(sess: Session, *, team_id: int) -> bytes:
    """Build and return the zip bundle as bytes."""
    team = sess.get(models.Team, team_id)
    if team is None:
        from .errors import NotFoundError
        raise NotFoundError("team not found")

    meetings = list(sess.execute(
        select(models.Meeting)
        .where(models.Meeting.team_id == team_id)
        .order_by(models.Meeting.id.asc())
    ).scalars().all())
    decisions = list(sess.execute(
        select(models.Decision)
        .where(models.Decision.team_id == team_id)
        .order_by(models.Decision.id.asc())
    ).scalars().all())
    tasks = list(sess.execute(
        select(models.Task)
        .where(models.Task.team_id == team_id)
        .order_by(models.Task.id.asc())
    ).scalars().all())
    wiki = list(sess.execute(
        select(models.WikiPage)
        .where(models.WikiPage.team_id == team_id)
        .order_by(models.WikiPage.slug.asc())
    ).scalars().all())
    audit_rows = list(sess.execute(
        select(models.AuditEvent)
        .where(models.AuditEvent.team_id == team_id)
        .order_by(models.AuditEvent.id.asc())
    ).scalars().all())

    manifest = {
        "labflow_version": __version__,
        "generated_at": now_utc().isoformat(),
        "team": {"id": team.id, "slug": team.slug, "name": team.name},
        "counts": {
            "meetings": len(meetings),
            "decisions": len(decisions),
            "tasks": len(tasks),
            "wiki_pages": len(wiki),
            "audit_events": len(audit_rows),
        },
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        # Audit log as line-delimited JSON, in chain order.
        audit_lines: list[str] = []
        for ev in audit_rows:
            audit_lines.append(json.dumps({
                "id": ev.id, "actor": ev.actor, "action": ev.action,
                "entity_type": ev.entity_type, "entity_id": ev.entity_id,
                "created_at": ev.created_at.isoformat() if ev.created_at else None,
                "metadata": json.loads(ev.metadata_json) if ev.metadata_json else None,
                "prev_hash": ev.prev_hash, "entry_hash": ev.entry_hash,
            }, sort_keys=True))
        zf.writestr("audit.jsonl", "\n".join(audit_lines) + ("\n" if audit_lines else ""))
        for m in meetings:
            zf.writestr(
                f"meetings/{m.id:06d}-{_slugify(m.title)}.md",
                _meeting_md(sess, m),
            )
        for d in decisions:
            zf.writestr(f"decisions/{d.id:06d}.md", _decision_md(d))
        for t in tasks:
            zf.writestr(f"tasks/{t.id:06d}-{_slugify(t.title)}.md", _task_md(t))
        for p in wiki:
            if not p.deleted:
                zf.writestr(f"wiki/{p.slug}.md", _wiki_md(p))
    return buf.getvalue()
