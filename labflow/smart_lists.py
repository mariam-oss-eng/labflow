"""Smart lists — saved declarative task filters (v0.13).

A smart list is a slug-addressed JSON filter. Supported keys (all
optional; missing keys are wildcards):

* ``state`` — workflow state key (e.g. ``in_progress``)
* ``status`` — legacy status (``open|in_progress|done|blocked``)
* ``assignee_handle`` — :class:`Owner.handle` (case-insensitive)
* ``priority`` — ``low|medium|high``
* ``label`` — substring match against task title (cheap, predictable)
* ``due_before`` — ISO-8601 date / datetime string
* ``sprint_slug`` — :class:`Sprint.slug`

Filters are validated on save and on every run; an unknown key raises
:class:`labflow.errors.ValidationError`.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit as audit_mod, models
from .errors import NotFoundError, ValidationError
from .time_utils import now_utc

ALLOWED_KEYS = frozenset({
    "state", "status", "assignee_handle", "priority",
    "label", "due_before", "sprint_slug",
})
VALID_PRIORITIES = frozenset({"low", "medium", "high"})


def _validate(filt: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(filt, dict):
        raise ValidationError("filter must be an object")
    bad = set(filt) - ALLOWED_KEYS
    if bad:
        raise ValidationError(
            f"unknown filter keys: {sorted(bad)}; allowed: {sorted(ALLOWED_KEYS)}"
        )
    if "priority" in filt and filt["priority"] not in VALID_PRIORITIES:
        raise ValidationError(
            f"priority must be one of {sorted(VALID_PRIORITIES)}"
        )
    if "due_before" in filt:
        try:
            datetime.fromisoformat(filt["due_before"])
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"invalid due_before: {exc}") from exc
    for k in ("state", "status", "assignee_handle", "label", "sprint_slug"):
        if k in filt and not isinstance(filt[k], str):
            raise ValidationError(f"{k} must be a string")
    return filt


def upsert(
    sess: Session, *, team_id: int, slug: str, name: str,
    filter: dict[str, Any], actor: str = "system",
) -> models.SmartList:
    s = (slug or "").strip().lower()
    if not s:
        raise ValidationError("slug is required")
    if not (name or "").strip():
        raise ValidationError("name is required")
    filt = _validate(filter)
    row = sess.execute(
        select(models.SmartList).where(
            models.SmartList.team_id == team_id,
            models.SmartList.slug == s,
        )
    ).scalar_one_or_none()
    if row is None:
        row = models.SmartList(
            team_id=team_id, slug=s, name=name.strip(),
            filter_json=json.dumps(filt, sort_keys=True),
        )
        sess.add(row)
    else:
        row.name = name.strip()
        row.filter_json = json.dumps(filt, sort_keys=True)
        row.updated_at = now_utc().replace(tzinfo=None)
    sess.flush()
    audit_mod.record(
        sess, team_id=team_id, action="smart_list.upserted",
        entity_type="smart_list", entity_id=row.id, actor=actor,
        metadata={"slug": s, "keys": sorted(filt.keys())},
    )
    return row


def list_all(sess: Session, *, team_id: int) -> list[models.SmartList]:
    return list(sess.execute(
        select(models.SmartList)
        .where(models.SmartList.team_id == team_id)
        .order_by(models.SmartList.slug.asc())
    ).scalars().all())


def get(sess: Session, *, team_id: int, slug: str) -> models.SmartList:
    row = sess.execute(
        select(models.SmartList).where(
            models.SmartList.team_id == team_id,
            models.SmartList.slug == slug,
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"smart list {slug!r} not found")
    return row


def delete(
    sess: Session, *, team_id: int, slug: str, actor: str = "system",
) -> None:
    row = get(sess, team_id=team_id, slug=slug)
    sess.delete(row)
    audit_mod.record(
        sess, team_id=team_id, action="smart_list.deleted",
        entity_type="smart_list", entity_id=row.id, actor=actor,
        metadata={"slug": slug},
    )


def run(
    sess: Session, *, team_id: int, slug: str, limit: int = 200,
) -> list[models.Task]:
    """Execute the named smart list and return matching tasks."""
    row = get(sess, team_id=team_id, slug=slug)
    filt: dict[str, Any] = json.loads(row.filter_json)

    q = select(models.Task).where(models.Task.team_id == team_id)
    if "state" in filt:
        q = q.where(models.Task.state == filt["state"])
    if "status" in filt:
        q = q.where(models.Task.status == filt["status"])
    if "priority" in filt:
        q = q.where(models.Task.priority == filt["priority"])
    if "label" in filt:
        like = f"%{filt['label']}%"
        q = q.where(models.Task.title.ilike(like))
    if "due_before" in filt:
        before = datetime.fromisoformat(filt["due_before"])
        q = q.where(models.Task.due_date <= before)
    if "assignee_handle" in filt:
        ow = sess.execute(
            select(models.Owner).where(
                models.Owner.team_id == team_id,
                models.Owner.handle.ilike(filt["assignee_handle"]),
            )
        ).scalar_one_or_none()
        if ow is None:
            return []
        q = q.where(models.Task.owner_id == ow.id)
    if "sprint_slug" in filt:
        sp = sess.execute(
            select(models.Sprint).where(
                models.Sprint.team_id == team_id,
                models.Sprint.slug == filt["sprint_slug"],
            )
        ).scalar_one_or_none()
        if sp is None:
            return []
        q = q.where(models.Task.sprint_id == sp.id)
    q = q.order_by(models.Task.id.desc()).limit(max(1, min(limit, 1000)))
    return list(sess.execute(q).scalars().all())
