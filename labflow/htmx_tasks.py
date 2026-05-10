"""HTMX-powered task list page (v0.15).

Self-contained, dependency-free server rendering — the page loads
``htmx@2`` from a CDN and uses it for inline status changes. Pure
``html.escape``-based templating; no Jinja, no client framework.

Two endpoints (wired in ``main.py``):

* ``GET /app/tasks``               — full page
* ``GET /api/tasks/_table``        — just the ``<table>`` HTMX swaps in
* ``POST /api/tasks/_status/{id}`` — change status, return refreshed row

The fragment endpoints return ``text/html`` because htmx's default
``hx-swap`` operates on HTML fragments.
"""
from __future__ import annotations

from html import escape

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models

_PAGE_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>LabFlow · Tasks</title>
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <script src="https://unpkg.com/htmx.org@2.0.4"
          integrity="sha384-HGfztofotfshcF7+8n44JQL2oJmowVChPTg48S+jvZoztPfvwD79OC/LTtG6dMp+"
          crossorigin="anonymous"></script>
  <style>
    body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;
         margin:0;background:#f5f7fa;color:#1f2937}
    header{background:#4f46e5;color:#fff;padding:1rem 1.5rem;
            display:flex;align-items:center;justify-content:space-between}
    header h1{margin:0;font-size:1.25rem;font-weight:600}
    .badge{background:rgba(255,255,255,.18);padding:.15rem .55rem;
            border-radius:99px;font-size:.75rem}
    main{padding:1.25rem 1.5rem;max-width:1100px;margin:0 auto}
    table{width:100%;border-collapse:collapse;background:#fff;
           border-radius:8px;overflow:hidden;
           box-shadow:0 1px 3px rgba(0,0,0,.06)}
    th,td{padding:.55rem .8rem;border-bottom:1px solid #eef0f4;
           text-align:left;font-size:.92rem}
    th{background:#f9fafb;font-weight:600;color:#475569;font-size:.8rem;
        text-transform:uppercase;letter-spacing:.04em}
    tr:last-child td{border-bottom:none}
    .pill{display:inline-block;padding:.1rem .55rem;border-radius:99px;
           font-size:.72rem;font-weight:600}
    .s-open{background:#fef3c7;color:#92400e}
    .s-in_progress{background:#dbeafe;color:#1e40af}
    .s-done{background:#d1fae5;color:#065f46}
    .s-blocked{background:#fee2e2;color:#991b1b}
    .actions button{margin-right:.25rem;padding:.2rem .55rem;
       border:1px solid #d1d5db;background:#fff;border-radius:5px;
       cursor:pointer;font-size:.78rem}
    .actions button:hover{background:#eef2ff}
    .filter-bar{margin-bottom:.85rem}
    .filter-bar input{padding:.45rem .65rem;border:1px solid #d1d5db;
       border-radius:6px;width:280px;font-size:.9rem}
    .empty{padding:1.5rem;text-align:center;color:#6b7280}
  </style>
</head>
<body>
  <header>
    <h1>🧪 LabFlow Tasks</h1>
    <span class="badge">__TEAM__</span>
  </header>
  <main>
    <div class="filter-bar">
      <input
        type="search" name="q" placeholder="Filter by title…"
        hx-get="/api/tasks/_table" hx-target="#task-table"
        hx-trigger="keyup changed delay:200ms, search"
        hx-include="[name='status']" />
      <select name="status"
              hx-get="/api/tasks/_table" hx-target="#task-table"
              hx-trigger="change"
              hx-include="[name='q']">
        <option value="">all statuses</option>
        <option value="open">open</option>
        <option value="in_progress">in_progress</option>
        <option value="done">done</option>
        <option value="blocked">blocked</option>
      </select>
    </div>
    <div id="task-table">__TABLE__</div>
  </main>
</body>
</html>
"""


_STATUS_NEXT = {
    "open": "in_progress",
    "in_progress": "done",
    "done": "open",
    "blocked": "open",
}


def _row_html(task: models.Task) -> str:
    status = task.status or "open"
    nxt = _STATUS_NEXT.get(status, "open")
    due = task.due_date.date().isoformat() if task.due_date else "—"
    return (
        f'<tr id="task-row-{task.id}">'
        f"<td>#{task.id}</td>"
        f"<td>{escape(task.title or '')}</td>"
        f'<td><span class="pill s-{escape(status)}">{escape(status)}</span></td>'
        f"<td>{escape(task.priority or '—')}</td>"
        f"<td>{escape(due)}</td>"
        f'<td class="actions">'
        f'  <button hx-post="/api/tasks/_status/{task.id}?to={escape(nxt)}"'
        f'          hx-target="#task-row-{task.id}" hx-swap="outerHTML">'
        f"    → {escape(nxt)}</button>"
        f"</td>"
        f"</tr>"
    )


def _table_html(tasks: list[models.Task]) -> str:
    if not tasks:
        return '<div class="empty">No tasks match the current filter.</div>'
    rows = "\n".join(_row_html(t) for t in tasks)
    return (
        "<table>"
        "<thead><tr>"
        "<th>ID</th><th>Title</th><th>Status</th>"
        "<th>Priority</th><th>Due</th><th></th>"
        "</tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
    )


def _query_tasks(
    sess: Session, *, team_id: int,
    q: str | None = None, status: str | None = None,
    limit: int = 100,
) -> list[models.Task]:
    qy = select(models.Task).where(models.Task.team_id == team_id)
    if status:
        qy = qy.where(models.Task.status == status)
    if q:
        like = f"%{q.strip()}%"
        qy = qy.where(models.Task.title.ilike(like))
    qy = qy.order_by(
        models.Task.due_date.is_(None),
        models.Task.due_date.asc(),
        models.Task.id.desc(),
    ).limit(limit)
    return list(sess.execute(qy).scalars().all())


def render_page(
    sess: Session, *, team_id: int, team_slug: str,
    q: str | None = None, status: str | None = None,
) -> str:
    tasks = _query_tasks(sess, team_id=team_id, q=q, status=status)
    return _PAGE_TEMPLATE.replace(
        "__TEAM__", escape(team_slug),
    ).replace(
        "__TABLE__", _table_html(tasks),
    )


def render_table_fragment(
    sess: Session, *, team_id: int,
    q: str | None = None, status: str | None = None,
) -> str:
    tasks = _query_tasks(sess, team_id=team_id, q=q, status=status)
    return _table_html(tasks)


def transition_status(
    sess: Session, *, team_id: int, task_id: int, to: str,
) -> str:
    """Update ``task.status`` and return the refreshed row HTML."""
    if to not in _STATUS_NEXT:
        from .errors import ValidationError
        raise ValidationError(f"unsupported target status {to!r}")
    task = sess.get(models.Task, task_id)
    if task is None or task.team_id != team_id:
        from .errors import NotFoundError
        raise NotFoundError(f"task {task_id} not found")
    task.status = to
    if to == "done" and task.closed_at is None:
        from .time_utils import now_utc
        task.closed_at = now_utc().replace(tzinfo=None)
    sess.flush()
    return _row_html(task)
