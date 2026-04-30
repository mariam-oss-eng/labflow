"""Task dependency graph + critical-path computation (v0.8).

LabFlow already supports task dependencies via the ``task_dependency``
association table. This module exposes them as a queryable DAG with the
*critical path* (longest dependency chain by estimated effort) computed
on demand. The endpoint is read-only — operators wire the DAG via
``POST /api/tasks/{id}/depends_on/{other_id}``.

Effort estimate
---------------
LabFlow doesn't store explicit effort. We approximate it using ``confidence``
as a proxy (low confidence = uncertain = budget more) plus a constant base.
This produces a *useful* ordering even without time estimates and can be
swapped for real effort later by adding a ``Task.effort_hours`` column.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .errors import ConflictError, NotFoundError, ValidationError


def _effort(task: models.Task) -> float:
    """Return a heuristic effort estimate in hours."""
    base = 4.0  # nominal half-day
    # Lower confidence => bigger spread; clamp to [4..16].
    return min(16.0, base + (1.0 - max(0.1, task.confidence)) * 12.0)


def _load_team_dag(sess: Session, *, team_id: int) -> tuple[dict[int, models.Task], dict[int, list[int]]]:
    tasks = {
        t.id: t for t in sess.execute(
            select(models.Task).where(models.Task.team_id == team_id)
        ).scalars()
    }
    edges: dict[int, list[int]] = defaultdict(list)
    rows = sess.execute(
        models.task_dependency.select()
    ).all()
    for r in rows:
        if r.task_id in tasks and r.depends_on_id in tasks:
            edges[r.depends_on_id].append(r.task_id)  # depends_on -> task
    return tasks, edges


def add_dependency(
    sess: Session, *, team_id: int, task_id: int, depends_on_id: int,
) -> None:
    """Insert a dependency edge after a cycle check."""
    if task_id == depends_on_id:
        raise ValidationError("a task cannot depend on itself")
    tasks, edges = _load_team_dag(sess, team_id=team_id)
    if task_id not in tasks or depends_on_id not in tasks:
        raise NotFoundError("task not found")
    # Cycle check: edges run "depends_on -> dependent" (precedence direction).
    # Adding ``task depends_on dst`` adds edge dst -> task. That's a cycle iff
    # task already precedes dst in the existing graph.
    if _reachable(edges, task_id, depends_on_id):
        raise ConflictError("dependency would create a cycle")
    sess.execute(
        models.task_dependency.insert().values(
            task_id=task_id, depends_on_id=depends_on_id,
        )
    )


def _reachable(edges: dict[int, list[int]], src: int, dst: int) -> bool:
    if src == dst:
        return True
    seen = {src}
    queue = deque([src])
    while queue:
        n = queue.popleft()
        for nxt in edges.get(n, []):
            if nxt == dst:
                return True
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return False


def topo_sort(tasks: dict[int, models.Task],
              edges: dict[int, list[int]]) -> list[int]:
    """Kahn's algorithm. Raises :class:`ValidationError` on cycle."""
    indeg = {tid: 0 for tid in tasks}
    for src, dsts in edges.items():
        for d in dsts:
            indeg[d] += 1
    queue = deque(tid for tid, n in indeg.items() if n == 0)
    order: list[int] = []
    while queue:
        n = queue.popleft()
        order.append(n)
        for nxt in edges.get(n, []):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if len(order) != len(tasks):
        raise ValidationError("dependency graph contains a cycle")
    return order


def critical_path(sess: Session, *, team_id: int) -> dict:
    """Compute the longest weighted path through the team's open tasks.

    Returns::

        {
          "nodes": [{"id", "title", "state", "effort_h"} ...],
          "edges": [{"from": id, "to": id} ...],
          "path":  [task_id, task_id, ...],   # critical path, source -> sink
          "total_effort_h": float,
        }
    """
    tasks, edges = _load_team_dag(sess, team_id=team_id)
    if not tasks:
        return {"nodes": [], "edges": [], "path": [], "total_effort_h": 0.0}
    order = topo_sort(tasks, edges)
    # dist[v] = max effort to reach end starting from v (longest-path DP).
    dist: dict[int, float] = {tid: _effort(t) for tid, t in tasks.items()}
    succ: dict[int, int | None] = {tid: None for tid in tasks}
    for n in reversed(order):
        for nxt in edges.get(n, []):
            cand = _effort(tasks[n]) + dist[nxt]
            if cand > dist[n]:
                dist[n] = cand
                succ[n] = nxt
    # Critical start = node with max dist.
    start = max(dist, key=lambda k: dist[k])
    path: list[int] = []
    cur: int | None = start
    seen: set[int] = set()
    while cur is not None and cur not in seen:
        seen.add(cur)
        path.append(cur)
        cur = succ[cur]
    return {
        "nodes": [
            {"id": t.id, "title": t.title, "state": t.state or t.status,
             "effort_h": round(_effort(t), 2)}
            for t in tasks.values()
        ],
        "edges": [
            {"from": src, "to": dst}
            for src, dsts in edges.items() for dst in dsts
        ],
        "path": path,
        "total_effort_h": round(dist[start], 2),
    }
