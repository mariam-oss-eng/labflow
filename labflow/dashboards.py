"""Customisable dashboards (v0.10).

A dashboard is a list of *widget specs* — small JSON objects describing
what data to fetch and how the UI should render them. The catalogue of
recognised widget kinds lives here; rendering is up to the client.

    {
      "kind": "open_tasks",  "params": {"limit": 10}
    }
    {
      "kind": "sprint_burndown", "params": {"slug": "sprint-1"}
    }
    {
      "kind": "recent_decisions", "params": {"limit": 5}
    }
    {
      "kind": "sla_breaches", "params": {}
    }
    {
      "kind": "automation_status", "params": {}
    }

The server owns this catalogue (so the client can stay dumb), and each
widget has a deterministic ``render`` function called from the
``/api/dashboards/{slug}/data`` endpoint to fill the widget body.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .errors import ConflictError, NotFoundError, ValidationError
from .time_utils import now_utc

WIDGET_RENDERERS: dict[str, Callable[..., dict]] = {}


def register(kind: str):
    def deco(fn):
        WIDGET_RENDERERS[kind] = fn
        return fn
    return deco


def widget_catalogue() -> list[dict]:
    return [
        {"kind": k,
         "doc": (fn.__doc__ or "").strip().splitlines()[0]
         if fn.__doc__ else ""}
        for k, fn in WIDGET_RENDERERS.items()
    ]


# ----------------------------------------------------------------- widgets
@register("open_tasks")
def _w_open_tasks(sess: Session, *, team_id: int, params: dict) -> dict:
    """List open tasks (status != closed), most-recent first."""
    limit = max(1, min(int(params.get("limit", 10)), 50))
    rows = sess.execute(
        select(models.Task).where(
            models.Task.team_id == team_id,
            models.Task.status != "closed",
        ).order_by(models.Task.id.desc()).limit(limit)
    ).scalars().all()
    return {
        "items": [
            {"id": t.id, "title": t.title, "status": t.status,
             "owner_id": t.owner_id, "due_date":
                 t.due_date.isoformat() if t.due_date else None}
            for t in rows
        ],
        "count": len(rows),
    }


@register("recent_decisions")
def _w_recent_decisions(sess: Session, *, team_id: int, params: dict) -> dict:
    """Most recent decisions (id descending)."""
    limit = max(1, min(int(params.get("limit", 5)), 25))
    rows = sess.execute(
        select(models.Decision).where(models.Decision.team_id == team_id)
        .order_by(models.Decision.id.desc()).limit(limit)
    ).scalars().all()
    return {
        "items": [
            {"id": d.id, "statement": d.statement, "rationale": d.rationale}
            for d in rows
        ],
        "count": len(rows),
    }


@register("sla_breaches")
def _w_sla_breaches(sess: Session, *, team_id: int, params: dict) -> dict:
    """Tasks whose ``sla_breach_at`` is in the past and aren't closed."""
    now = now_utc()
    rows = sess.execute(
        select(models.Task).where(
            models.Task.team_id == team_id,
            models.Task.sla_breach_at.is_not(None),
            models.Task.sla_breach_at <= now,
            models.Task.status != "closed",
        ).order_by(models.Task.sla_breach_at.asc())
    ).scalars().all()
    return {
        "items": [
            {"id": t.id, "title": t.title, "status": t.status,
             "breach_at": t.sla_breach_at.isoformat()}
            for t in rows
        ],
        "count": len(rows),
    }


@register("sprint_burndown")
def _w_sprint_burndown(sess: Session, *, team_id: int, params: dict) -> dict:
    """Burndown series for the requested sprint slug."""
    slug = params.get("slug")
    if not slug:
        return {"error": "missing 'slug' param"}
    from . import sprints as sprints_mod
    try:
        return sprints_mod.burndown(sess, team_id=team_id, slug=slug)
    except NotFoundError as exc:
        return {"error": str(exc)}


@register("automation_status")
def _w_automation_status(sess: Session, *, team_id: int, params: dict) -> dict:
    """Counts of enabled / disabled rules and total fires this team."""
    rows = sess.execute(
        select(models.AutomationRule).where(
            models.AutomationRule.team_id == team_id
        )
    ).scalars().all()
    enabled = sum(1 for r in rows if r.enabled)
    return {
        "rules": len(rows),
        "enabled": enabled,
        "disabled": len(rows) - enabled,
        "total_fires": sum(r.fires for r in rows),
    }


# ------------------------------------------------------------------ CRUD
def _validate_layout(layout: list) -> None:
    if not isinstance(layout, list):
        raise ValidationError("layout must be a list")
    for w in layout:
        if not isinstance(w, dict) or "kind" not in w:
            raise ValidationError("each widget needs a 'kind'")
        if w["kind"] not in WIDGET_RENDERERS:
            raise ValidationError(
                f"unknown widget kind '{w['kind']}'; "
                f"valid: {sorted(WIDGET_RENDERERS)}"
            )


def upsert_dashboard(
    sess: Session, *, team_id: int, owner_key_id: int | None,
    slug: str, name: str, layout: list, is_default: bool = False,
) -> models.Dashboard:
    if not slug:
        raise ValidationError("slug is required")
    _validate_layout(layout)
    existing = sess.execute(
        select(models.Dashboard).where(
            models.Dashboard.team_id == team_id,
            models.Dashboard.owner_key_id == owner_key_id,
            models.Dashboard.slug == slug,
        )
    ).scalar_one_or_none()
    now = now_utc()
    if existing is not None:
        existing.name = name
        existing.layout_json = json.dumps(layout)
        existing.is_default = is_default
        existing.updated_at = now
        sess.flush()
        return existing
    db = models.Dashboard(
        team_id=team_id, owner_key_id=owner_key_id, slug=slug, name=name,
        layout_json=json.dumps(layout), is_default=is_default,
        created_at=now, updated_at=now,
    )
    sess.add(db)
    sess.flush()
    return db


def list_dashboards(sess: Session, *, team_id: int) -> list[models.Dashboard]:
    return list(sess.execute(
        select(models.Dashboard).where(models.Dashboard.team_id == team_id)
        .order_by(models.Dashboard.id.asc())
    ).scalars())


def get_dashboard(sess: Session, *, team_id: int, slug: str) -> models.Dashboard:
    db = sess.execute(
        select(models.Dashboard).where(
            models.Dashboard.team_id == team_id,
            models.Dashboard.slug == slug,
        ).order_by(models.Dashboard.id.asc()).limit(1)
    ).scalar_one_or_none()
    if db is None:
        raise NotFoundError(f"dashboard {slug!r} not found")
    return db


def render(sess: Session, *, team_id: int, slug: str) -> dict:
    db = get_dashboard(sess, team_id=team_id, slug=slug)
    layout = json.loads(db.layout_json)
    rendered = []
    for w in layout:
        kind = w["kind"]
        params = w.get("params") or {}
        try:
            data = WIDGET_RENDERERS[kind](sess, team_id=team_id, params=params)
        except Exception as exc:  # noqa: BLE001
            data = {"error": str(exc)}
        rendered.append({"kind": kind, "params": params, "data": data})
    return {
        "slug": db.slug, "name": db.name,
        "is_default": db.is_default,
        "widgets": rendered,
    }
