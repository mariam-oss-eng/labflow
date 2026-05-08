"""Bulk task operations (v0.12).

A single endpoint that lets callers update many tasks in one
transactional request. Each operation goes through the *same* code path
as its single-task equivalent so audit/automation/webhooks fire
identically — we just batch the database round-trip.

Supported operations (one per call):

* ``transition`` — workflow state transition (delegates to
  :mod:`labflow.workflows`).
* ``assign`` — set ``owner_id``.
* ``set_priority`` — set ``priority`` to ``low|medium|high``.
* ``set_status`` — set legacy ``status`` (e.g. ``open|in_progress|done``);
  setting to ``done`` also stamps ``closed_at``.

Returns a per-task result row so partial failures are visible without
poisoning the rest of the batch (the whole batch still commits in one
transaction; failed rows roll back their own field changes only).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from . import audit as audit_mod, models, workflows as workflows_mod
from .errors import NotFoundError, ValidationError
from .time_utils import now_utc

VALID_OPS = frozenset({"transition", "assign", "set_priority", "set_status"})
VALID_PRIORITIES = frozenset({"low", "medium", "high"})


def _result(task_id: int, ok: bool, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"task_id": task_id, "ok": ok}
    out.update(extra)
    return out


def apply_bulk(
    sess: Session, *, team_id: int, op: str, task_ids: list[int],
    args: dict[str, Any] | None = None, actor: str = "system",
) -> dict[str, Any]:
    """Apply ``op`` to the given task ids. Returns a summary dict.

    The shape is::

        {
            "op": "...",
            "applied": int,       # number of successful tasks
            "failed":  int,
            "results": [ {task_id, ok, error?, from?, to?}, ... ],
        }
    """
    if op not in VALID_OPS:
        raise ValidationError(f"unsupported op {op!r}; allowed: {sorted(VALID_OPS)}")
    if not isinstance(task_ids, list) or not task_ids:
        raise ValidationError("task_ids must be a non-empty list")
    if len(task_ids) > 500:
        raise ValidationError("bulk batch size limited to 500 tasks per call")
    args = dict(args or {})

    results: list[dict[str, Any]] = []
    applied = 0
    failed = 0

    for tid in task_ids:
        task = sess.get(models.Task, tid)
        if task is None or task.team_id != team_id:
            results.append(_result(tid, False, error="not_found"))
            failed += 1
            continue
        sp = sess.begin_nested()
        try:
            if op == "transition":
                to_state = args.get("to")
                if not to_state:
                    raise ValidationError("transition op requires 'to'")
                tr = workflows_mod.transition_task(
                    sess, team_id=team_id, task_id=tid, to_state=to_state,
                    actor=actor, reason=args.get("reason"),
                )
                results.append(_result(tid, True, **{"from": tr.from_state, "to": tr.to_state}))
            elif op == "assign":
                owner_id = args.get("owner_id")
                if owner_id is not None and not isinstance(owner_id, int):
                    raise ValidationError("owner_id must be an int or null")
                prev = task.owner_id
                task.owner_id = owner_id
                audit_mod.record(
                    sess, team_id=team_id, action="task.assigned",
                    entity_type="task", entity_id=tid, actor=actor,
                    metadata={"from": prev, "to": owner_id},
                )
                results.append(_result(tid, True, **{"from": prev, "to": owner_id}))
            elif op == "set_priority":
                pr = args.get("priority")
                if pr is not None and pr not in VALID_PRIORITIES:
                    raise ValidationError(
                        f"priority must be one of {sorted(VALID_PRIORITIES)} or null"
                    )
                prev = task.priority
                task.priority = pr
                audit_mod.record(
                    sess, team_id=team_id, action="task.priority_set",
                    entity_type="task", entity_id=tid, actor=actor,
                    metadata={"from": prev, "to": pr},
                )
                results.append(_result(tid, True, **{"from": prev, "to": pr}))
            elif op == "set_status":
                st = args.get("status")
                if not isinstance(st, str) or not st:
                    raise ValidationError("status must be a non-empty string")
                prev = task.status
                if st == "done" and prev != "done":
                    task.closed_at = now_utc().replace(tzinfo=None)
                task.status = st
                audit_mod.record(
                    sess, team_id=team_id, action="task.status_changed",
                    entity_type="task", entity_id=tid, actor=actor,
                    metadata={"from": prev, "to": st},
                )
                results.append(_result(tid, True, **{"from": prev, "to": st}))
            sess.flush()
            sp.commit()
            applied += 1
        except Exception as exc:  # noqa: BLE001
            sp.rollback()
            results.append(_result(tid, False, error=str(exc)))
            failed += 1

    return {
        "op": op, "applied": applied, "failed": failed, "results": results,
    }
