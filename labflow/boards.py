"""Kanban-style task board (v0.12).

Groups a team's tasks into the columns of a configured workflow's state
machine. Pure read API + a thin HTML renderer; the *state transitions*
themselves continue to flow through the existing
``POST /api/tasks/{id}/transition`` route so audit, webhooks, and
automation rules all run identically.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models, workflows as workflows_mod


def _task_card(t: models.Task) -> dict[str, Any]:
    return {
        "id": t.id,
        "title": t.title,
        "owner_id": t.owner_id,
        "kind": t.kind,
        "due_date": t.due_date.isoformat() if t.due_date else None,
        "status": t.status,
        "state": t.state,
        "sprint_id": t.sprint_id,
    }


def board_for_workflow(
    sess: Session, *, team_id: int, workflow_slug: str | None = None,
) -> dict[str, Any]:
    """Return ``{"workflow": {...}, "columns": [{state, name, tasks: [...]}]}``.

    ``workflow_slug`` matches against :attr:`Workflow.name` (workflows
    are name-addressed in v0.8). ``None`` picks the team's default.
    Tasks not yet in the workflow's state space (e.g. legacy tasks
    created before v0.8) fall into a synthetic ``inbox`` column so
    they're still visible.
    """
    if workflow_slug:
        wf = sess.execute(
            select(models.Workflow).where(
                models.Workflow.team_id == team_id,
                models.Workflow.name == workflow_slug,
            )
        ).scalar_one_or_none()
        if wf is None:
            from .errors import NotFoundError
            raise NotFoundError(f"workflow {workflow_slug!r} not found")
    else:
        wf = workflows_mod.get_default_workflow(sess, team_id=team_id)

    definition = workflows_mod._load(wf)
    raw_states = definition.get("states") or []
    # Workflow states may be either bare strings (default) or dicts with
    # ``key``/``name``/``kind`` (advanced). Normalise to dicts so the
    # rendering layer doesn't need to branch.
    states: list[dict[str, Any]] = []
    terminal = set(definition.get("terminal") or [])
    for s in raw_states:
        if isinstance(s, str):
            states.append({
                "key": s,
                "name": s.replace("_", " ").title(),
                "kind": "done" if s in terminal else "open",
            })
        elif isinstance(s, dict) and "key" in s:
            states.append({
                "key": s["key"],
                "name": s.get("name", s["key"]),
                "kind": s.get("kind", "done" if s["key"] in terminal else "open"),
            })
    state_keys = {s["key"] for s in states}

    # Bucket tasks by their workflow state.
    tasks = sess.execute(
        select(models.Task)
        .where(models.Task.team_id == team_id)
        .order_by(models.Task.id.asc())
    ).scalars().all()

    buckets: dict[str, list[dict[str, Any]]] = {s["key"]: [] for s in states}
    inbox: list[dict[str, Any]] = []
    for t in tasks:
        key = t.state if t.state in state_keys else None
        if key:
            buckets[key].append(_task_card(t))
        else:
            inbox.append(_task_card(t))

    columns: list[dict[str, Any]] = []
    for s in states:
        columns.append({
            "state": s["key"],
            "name": s.get("name", s["key"]),
            "kind": s.get("kind", "open"),
            "tasks": buckets[s["key"]],
            "count": len(buckets[s["key"]]),
        })
    if inbox:
        columns.insert(0, {
            "state": "inbox", "name": "Inbox", "kind": "open",
            "tasks": inbox, "count": len(inbox),
        })

    return {
        "workflow": {"name": wf.name, "id": wf.id},
        "columns": columns,
        "total": sum(c["count"] for c in columns),
    }


# ---------------------------------------------------------------------------
# HTML rendering — kept dependency-free, no template engine used.
# ---------------------------------------------------------------------------

_PAGE_CSS = """
body{font:14px system-ui,sans-serif;margin:0;background:#0f172a;color:#e2e8f0}
header{padding:12px 20px;background:#020617;display:flex;align-items:center;
       gap:14px;border-bottom:1px solid #1e293b}
header h1{font-size:18px;margin:0}
.board{display:flex;gap:14px;padding:18px;overflow-x:auto;min-height:90vh}
.col{min-width:280px;max-width:320px;background:#1e293b;border-radius:8px;
     display:flex;flex-direction:column}
.col h2{font-size:13px;letter-spacing:.04em;text-transform:uppercase;
        margin:0;padding:10px 12px;border-bottom:1px solid #334155;
        display:flex;justify-content:space-between;align-items:center}
.col h2 .badge{background:#334155;color:#cbd5e1;border-radius:999px;
               padding:1px 8px;font-size:11px}
.cards{padding:8px;display:flex;flex-direction:column;gap:8px;
       overflow-y:auto}
.card{background:#0f172a;border:1px solid #334155;border-radius:6px;
      padding:10px;cursor:default}
.card .t{font-weight:600;color:#f1f5f9;margin-bottom:4px}
.card .meta{font-size:12px;color:#94a3b8;display:flex;gap:10px;
            flex-wrap:wrap}
.priority-high{border-left:3px solid #f87171}
.priority-medium{border-left:3px solid #fbbf24}
.priority-low{border-left:3px solid #34d399}
.kind-done{opacity:.55}
"""


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_html(board: dict[str, Any], *, team_slug: str) -> str:
    """Render a self-contained HTML page for the kanban board."""
    cols_html: list[str] = []
    for col in board["columns"]:
        cards_html: list[str] = []
        for t in col["tasks"]:
            meta_parts: list[str] = []
            if t.get("due_date"):
                meta_parts.append(f"📅 {_esc(t['due_date'])}")
            if t.get("owner_id") is not None:
                meta_parts.append(f"👤 #{t['owner_id']}")
            meta_parts.append(f"#{t['id']}")
            cards_html.append(
                f'<div class="card">'
                f'<div class="t">{_esc(t["title"])}</div>'
                f'<div class="meta">{" · ".join(meta_parts)}</div>'
                f'</div>'
            )
        kclass = f" kind-{col.get('kind')}" if col.get("kind") else ""
        cols_html.append(
            f'<div class="col{kclass}">'
            f'<h2><span>{_esc(col["name"])}</span>'
            f'<span class="badge">{col["count"]}</span></h2>'
            f'<div class="cards">{"".join(cards_html)}</div>'
            f'</div>'
        )

    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>LabFlow board · {_esc(team_slug)}</title>"
        f"<style>{_PAGE_CSS}</style></head><body>"
        f"<header><h1>📋 {_esc(board['workflow']['name'])}</h1>"
        f"<span style='color:#94a3b8'>{board['total']} tasks · "
        f"team <code>{_esc(team_slug)}</code></span></header>"
        f"<div class='board'>{''.join(cols_html)}</div>"
        "</body></html>"
    )
