"""Configurable task state-machine engine (v0.8).

A *workflow* is a JSON document describing the legal states a task can be
in and the legal transitions between them. Each team has a default
workflow (auto-created on first use) and may define alternates.

Design notes
------------
* Workflows are *data*, not code — operators configure them via the API
  with no deploy required. The engine evaluates guards (role thresholds)
  and triggers hooks (audit + SSE) generically.
* SLA support: a transition with ``sla_hours`` causes ``Task.sla_breach_at``
  to be stamped to ``now + sla_hours``; a sweep job (``sla_sweep``) escalates
  breached tasks by emitting a typed event ``task.sla.breach``.
* Backwards compatible: when no workflow is set, ``Task.state`` is None
  and the legacy ``Task.status`` field continues to work. When a workflow
  is attached, the engine keeps both fields in sync (``status`` mirrors
  the terminal/non-terminal coarse state).

The default workflow is a 5-state research pipeline::

    open → in_progress → in_review → blocked → closed
                ↑__________________________|
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit as audit_mod, models, sse as sse_mod
from .auth import ROLE_HIERARCHY
from .errors import ConflictError, NotFoundError, ValidationError
from .time_utils import now_utc


# Maximum reasonable SLA window — guards against accidental year-long SLAs
# from typoed input.
_MAX_SLA_HOURS = 24 * 365


DEFAULT_WORKFLOW: dict[str, Any] = {
    "states": ["open", "in_progress", "in_review", "blocked", "closed"],
    "initial": "open",
    "terminal": ["closed"],
    "transitions": [
        {"from": "open", "to": "in_progress", "guard_role": "member"},
        {"from": "in_progress", "to": "in_review",
         "guard_role": "member", "sla_hours": 48},
        {"from": "in_review", "to": "closed", "guard_role": "member"},
        {"from": "in_progress", "to": "blocked", "guard_role": "member"},
        {"from": "blocked", "to": "in_progress", "guard_role": "member"},
        {"from": "in_review", "to": "in_progress", "guard_role": "member"},
        # Admin escape hatch: any state to closed
        {"from": "*", "to": "closed", "guard_role": "admin"},
    ],
}


# --------------------------------------------------------------------------- validation

def validate_definition(definition: dict[str, Any]) -> None:
    """Raise :class:`ValidationError` if ``definition`` isn't a workflow.

    Checks: states is a non-empty list of strings; initial in states; terminal
    rows in states; every transition.from is in states or "*"; every .to is
    in states; sla_hours is a positive int <= MAX.
    """
    if not isinstance(definition, dict):
        raise ValidationError("workflow definition must be an object")
    states = definition.get("states")
    if (not isinstance(states, list) or not states
            or not all(isinstance(s, str) and s for s in states)):
        raise ValidationError("workflow.states must be a non-empty list of strings")
    if len(set(states)) != len(states):
        raise ValidationError("workflow.states must be unique")
    initial = definition.get("initial")
    if initial not in states:
        raise ValidationError("workflow.initial must be one of states")
    terminal = definition.get("terminal", [])
    if not isinstance(terminal, list) or any(s not in states for s in terminal):
        raise ValidationError("workflow.terminal must be a subset of states")
    transitions = definition.get("transitions", [])
    if not isinstance(transitions, list):
        raise ValidationError("workflow.transitions must be a list")
    for i, t in enumerate(transitions):
        if not isinstance(t, dict):
            raise ValidationError(f"transition[{i}] must be an object")
        f, to = t.get("from"), t.get("to")
        if f != "*" and f not in states:
            raise ValidationError(f"transition[{i}].from invalid: {f!r}")
        if to not in states:
            raise ValidationError(f"transition[{i}].to invalid: {to!r}")
        guard = t.get("guard_role", "member")
        if guard not in ROLE_HIERARCHY:
            raise ValidationError(f"transition[{i}].guard_role invalid: {guard!r}")
        sla = t.get("sla_hours")
        if sla is not None:
            if (not isinstance(sla, (int, float)) or sla <= 0
                    or sla > _MAX_SLA_HOURS):
                raise ValidationError(
                    f"transition[{i}].sla_hours must be 0<x<={_MAX_SLA_HOURS}"
                )


# --------------------------------------------------------------------------- CRUD

def get_default_workflow(sess: Session, *, team_id: int) -> models.Workflow:
    """Return the team's default workflow, creating one on first use."""
    wf = sess.execute(
        select(models.Workflow).where(
            models.Workflow.team_id == team_id,
            models.Workflow.is_default.is_(True),
        )
    ).scalar_one_or_none()
    if wf is None:
        wf = models.Workflow(
            team_id=team_id,
            name="default",
            definition_json=json.dumps(DEFAULT_WORKFLOW),
            is_default=True,
        )
        sess.add(wf)
        sess.flush()
    return wf


def create_workflow(
    sess: Session, *, team_id: int, name: str,
    definition: dict[str, Any], make_default: bool = False,
    actor: str = "system",
) -> models.Workflow:
    if not name or not name.strip():
        raise ValidationError("workflow name required")
    name = name.strip()
    validate_definition(definition)
    existing = sess.execute(
        select(models.Workflow).where(
            models.Workflow.team_id == team_id,
            models.Workflow.name == name,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(f"workflow {name!r} already exists")
    if make_default:
        # Demote prior defaults — only one default per team.
        for prior in sess.execute(
            select(models.Workflow).where(
                models.Workflow.team_id == team_id,
                models.Workflow.is_default.is_(True),
            )
        ).scalars():
            prior.is_default = False
    wf = models.Workflow(
        team_id=team_id, name=name,
        definition_json=json.dumps(definition),
        is_default=make_default,
    )
    sess.add(wf)
    sess.flush()
    audit_mod.record(sess, team_id=team_id, actor=actor,
                     action="workflow.create",
                     entity_type="workflow", entity_id=wf.id,
                     metadata={"name": name})
    return wf


def list_workflows(sess: Session, *, team_id: int) -> list[models.Workflow]:
    return list(sess.execute(
        select(models.Workflow).where(models.Workflow.team_id == team_id)
        .order_by(models.Workflow.is_default.desc(), models.Workflow.name)
    ).scalars())


# --------------------------------------------------------------------------- transitions

def _load(wf: models.Workflow) -> dict[str, Any]:
    return json.loads(wf.definition_json)


def _find_transition(definition: dict, *, frm: str, to: str) -> dict | None:
    for t in definition.get("transitions", []):
        if (t["from"] == frm or t["from"] == "*") and t["to"] == to:
            return t
    return None


@dataclass
class TransitionResult:
    task: models.Task
    from_state: str
    to_state: str
    sla_breach_at: Any
    is_terminal: bool


def transition_task(
    sess: Session, *, task: models.Task, to_state: str,
    actor: str, actor_role: str = "admin",
    publish: bool = True,
) -> TransitionResult:
    """Move ``task`` to ``to_state`` if the workflow allows it.

    Raises:
      NotFoundError: if the task has no workflow attached and the team has
        no default workflow available.
      ValidationError: if ``to_state`` is not a legal next state.
      ConflictError: if the actor's role is below the transition's guard.
    """
    if task.workflow_id is None:
        wf = get_default_workflow(sess, team_id=task.team_id)
        task.workflow_id = wf.id
        if task.state is None:
            task.state = _load(wf)["initial"]
    else:
        wf = sess.get(models.Workflow, task.workflow_id)
        if wf is None:
            raise NotFoundError("task workflow not found")
    definition = _load(wf)
    if to_state not in definition["states"]:
        raise ValidationError(f"unknown state {to_state!r}")
    from_state = task.state or definition["initial"]
    if from_state == to_state:
        # No-op; do not error so idempotent retries succeed.
        return TransitionResult(task, from_state, to_state,
                                task.sla_breach_at,
                                to_state in definition.get("terminal", []))
    transition = _find_transition(definition, frm=from_state, to=to_state)
    if transition is None:
        raise ValidationError(
            f"no transition from {from_state!r} to {to_state!r}"
        )
    guard = transition.get("guard_role", "member")
    if ROLE_HIERARCHY.get(actor_role, -1) < ROLE_HIERARCHY[guard]:
        raise ConflictError(
            f"role {actor_role!r} cannot transition to {to_state!r} "
            f"(need {guard!r})"
        )
    task.state = to_state
    sla = transition.get("sla_hours")
    if sla is not None:
        task.sla_breach_at = now_utc().replace(tzinfo=None) + timedelta(hours=float(sla))
    else:
        task.sla_breach_at = None
    is_terminal = to_state in definition.get("terminal", [])
    if is_terminal:
        task.status = "closed"
        if task.closed_at is None:
            task.closed_at = now_utc().replace(tzinfo=None)
    else:
        task.status = to_state if to_state in {"open", "in_progress"} else "open"

    audit_mod.record(
        sess, team_id=task.team_id, actor=actor,
        action="task.transition", entity_type="task", entity_id=task.id,
        metadata={"from": from_state, "to": to_state,
                  "sla_breach_at": task.sla_breach_at.isoformat()
                  if task.sla_breach_at else None},
    )
    if publish:
        try:
            sse_mod.hub().publish(task.team_id, "task.transition", {
                "task_id": task.id, "from": from_state, "to": to_state,
                "sla_breach_at": task.sla_breach_at.isoformat()
                if task.sla_breach_at else None,
            })
        except Exception:  # noqa: BLE001
            pass
    return TransitionResult(task, from_state, to_state,
                            task.sla_breach_at, is_terminal)


# --------------------------------------------------------------------------- SLA sweep

def sweep_sla_breaches(sess: Session, *, team_id: int | None = None) -> list[dict]:
    """Find tasks whose ``sla_breach_at`` has passed and emit escalation events.

    Returns one dict per task: ``{task_id, state, breach_at}``. The function
    *resets* ``sla_breach_at`` to None after emitting so we don't escalate
    repeatedly. Operators run this periodically via a cron / k8s job calling
    ``POST /api/admin/sla/sweep``.
    """
    cutoff = now_utc().replace(tzinfo=None)
    q = select(models.Task).where(
        models.Task.sla_breach_at.is_not(None),
        models.Task.sla_breach_at <= cutoff,
        models.Task.status != "closed",
    )
    if team_id is not None:
        q = q.where(models.Task.team_id == team_id)
    breached: list[dict] = []
    for task in sess.execute(q).scalars():
        evt = {
            "task_id": task.id,
            "state": task.state,
            "breach_at": task.sla_breach_at.isoformat(),
            "owner_id": task.owner_id,
        }
        breached.append(evt)
        audit_mod.record(
            sess, team_id=task.team_id, actor="sla-sweep",
            action="task.sla.breach", entity_type="task", entity_id=task.id,
            metadata=evt,
        )
        try:
            sse_mod.hub().publish(task.team_id, "task.sla.breach", evt)
        except Exception:  # noqa: BLE001
            pass
        task.sla_breach_at = None
    return breached
