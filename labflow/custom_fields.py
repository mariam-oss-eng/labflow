"""Per-team custom fields (v0.16).

Operators define a small schema of extra fields per entity kind
(``task`` or ``decision``) that the typed graph doesn't natively model
— things like ``epic``, ``cost_center``, ``risk_level``. Field values
attach to individual rows.

Design
------
* Fields are scoped to ``(team_id, entity_type)`` and identified by a
  ``key`` (URL-safe, lower-snake-case).
* Four kinds: ``text``, ``number``, ``date``, ``select``. Validation
  is done both on save (def → ensure ``options`` is a non-empty list
  for ``select``) and on each value write (parse + range check).
* Values are stored as strings; the typed value comes back via
  :func:`get_typed_value` so the API can return e.g. floats / ISO dates.
* Every CRUD operation appends an audit row, matching the rest of the
  codebase.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit as audit_mod, models
from .errors import ConflictError, NotFoundError, ValidationError
from .time_utils import now_utc

VALID_ENTITIES = frozenset({"task", "decision"})
VALID_KINDS = frozenset({"text", "number", "date", "select"})
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_TEXT_MAX = 4096


def _validate_def(*, entity_type: str, key: str, kind: str,
                  options: list[str] | None) -> str | None:
    if entity_type not in VALID_ENTITIES:
        raise ValidationError(
            f"entity_type must be one of {sorted(VALID_ENTITIES)}"
        )
    if kind not in VALID_KINDS:
        raise ValidationError(f"kind must be one of {sorted(VALID_KINDS)}")
    if not _KEY_RE.match(key or ""):
        raise ValidationError(
            "key must be lower-snake-case, 1–63 chars, start with a letter"
        )
    if kind == "select":
        if not isinstance(options, list) or not options:
            raise ValidationError("select fields require non-empty options")
        if not all(isinstance(o, str) and o for o in options):
            raise ValidationError("options must be non-empty strings")
        if len(options) != len(set(options)):
            raise ValidationError("options must be unique")
        return json.dumps(options)
    if options:
        raise ValidationError("options only allowed for kind=select")
    return None


def define_field(
    sess: Session, *, team_id: int, entity_type: str, key: str,
    label: str, kind: str, options: list[str] | None = None,
    required: bool = False, actor: str = "system",
) -> models.CustomFieldDef:
    """Create (or 409 on conflict) a new field definition."""
    options_json = _validate_def(
        entity_type=entity_type, key=key, kind=kind, options=options,
    )
    existing = sess.execute(
        select(models.CustomFieldDef).where(
            models.CustomFieldDef.team_id == team_id,
            models.CustomFieldDef.entity_type == entity_type,
            models.CustomFieldDef.key == key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(f"field {entity_type}.{key} already defined")
    row = models.CustomFieldDef(
        team_id=team_id, entity_type=entity_type, key=key,
        label=label or key, kind=kind, options_json=options_json,
        required=bool(required),
    )
    sess.add(row)
    sess.flush()
    audit_mod.record(
        sess, team_id=team_id, action="custom_field.define",
        entity_type="custom_field_def", entity_id=row.id, actor=actor,
        metadata={"entity_type": entity_type, "key": key, "kind": kind},
    )
    return row


def list_fields(
    sess: Session, *, team_id: int, entity_type: str | None = None,
) -> list[models.CustomFieldDef]:
    q = select(models.CustomFieldDef).where(
        models.CustomFieldDef.team_id == team_id,
    )
    if entity_type is not None:
        if entity_type not in VALID_ENTITIES:
            raise ValidationError(
                f"entity_type must be one of {sorted(VALID_ENTITIES)}"
            )
        q = q.where(models.CustomFieldDef.entity_type == entity_type)
    return list(sess.execute(q.order_by(models.CustomFieldDef.id)).scalars())


def delete_field(
    sess: Session, *, team_id: int, def_id: int, actor: str = "system",
) -> None:
    row = sess.get(models.CustomFieldDef, def_id)
    if row is None or row.team_id != team_id:
        raise NotFoundError(f"field def {def_id} not found")
    sess.delete(row)
    sess.flush()
    audit_mod.record(
        sess, team_id=team_id, action="custom_field.delete",
        entity_type="custom_field_def", entity_id=def_id, actor=actor,
    )


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------
def _coerce_value(field: models.CustomFieldDef, raw: Any) -> str:
    if field.kind == "text":
        if not isinstance(raw, str):
            raise ValidationError(f"{field.key}: expected string")
        if len(raw) > _TEXT_MAX:
            raise ValidationError(
                f"{field.key}: value too long ({len(raw)} > {_TEXT_MAX})"
            )
        return raw
    if field.kind == "number":
        try:
            return str(float(raw))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field.key}: not a number") from exc
    if field.kind == "date":
        if not isinstance(raw, str):
            raise ValidationError(f"{field.key}: date must be ISO-8601 string")
        try:
            datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValidationError(f"{field.key}: invalid date") from exc
        return raw
    if field.kind == "select":
        opts = json.loads(field.options_json or "[]")
        if raw not in opts:
            raise ValidationError(
                f"{field.key}: must be one of {opts}"
            )
        return raw
    raise AssertionError(f"unknown kind {field.kind!r}")


def _ensure_entity(sess: Session, *, team_id: int,
                   entity_type: str, entity_id: int) -> None:
    table = {"task": models.Task,
             "decision": models.Decision}[entity_type]
    row = sess.get(table, entity_id)
    if row is None or row.team_id != team_id:
        raise NotFoundError(f"{entity_type} {entity_id} not found")


def set_value(
    sess: Session, *, team_id: int, entity_type: str, entity_id: int,
    key: str, value: Any, actor: str = "system",
) -> models.CustomFieldValue:
    _ensure_entity(
        sess, team_id=team_id, entity_type=entity_type, entity_id=entity_id,
    )
    field = sess.execute(
        select(models.CustomFieldDef).where(
            models.CustomFieldDef.team_id == team_id,
            models.CustomFieldDef.entity_type == entity_type,
            models.CustomFieldDef.key == key,
        )
    ).scalar_one_or_none()
    if field is None:
        raise NotFoundError(f"field {entity_type}.{key} not defined")
    coerced = _coerce_value(field, value)

    existing = sess.execute(
        select(models.CustomFieldValue).where(
            models.CustomFieldValue.def_id == field.id,
            models.CustomFieldValue.entity_id == entity_id,
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = models.CustomFieldValue(
            team_id=team_id, def_id=field.id, entity_type=entity_type,
            entity_id=entity_id, value=coerced,
        )
        sess.add(existing)
    else:
        existing.value = coerced
        existing.updated_at = now_utc()
    sess.flush()
    audit_mod.record(
        sess, team_id=team_id, action="custom_field.set",
        entity_type=entity_type, entity_id=entity_id, actor=actor,
        metadata={"key": key},
    )
    return existing


def get_typed_value(field: models.CustomFieldDef, raw: str) -> Any:
    if field.kind == "number":
        return float(raw)
    if field.kind == "date":
        return raw  # already ISO-8601; clients can parse
    return raw


def list_values(
    sess: Session, *, team_id: int, entity_type: str, entity_id: int,
) -> dict[str, Any]:
    """Return a ``{key: typed_value}`` dict for one entity."""
    rows = sess.execute(
        select(models.CustomFieldValue, models.CustomFieldDef)
        .join(models.CustomFieldDef,
              models.CustomFieldDef.id == models.CustomFieldValue.def_id)
        .where(
            models.CustomFieldValue.team_id == team_id,
            models.CustomFieldValue.entity_type == entity_type,
            models.CustomFieldValue.entity_id == entity_id,
        )
    ).all()
    return {f.key: get_typed_value(f, v.value) for v, f in rows}


def delete_value(
    sess: Session, *, team_id: int, entity_type: str, entity_id: int,
    key: str, actor: str = "system",
) -> None:
    field = sess.execute(
        select(models.CustomFieldDef).where(
            models.CustomFieldDef.team_id == team_id,
            models.CustomFieldDef.entity_type == entity_type,
            models.CustomFieldDef.key == key,
        )
    ).scalar_one_or_none()
    if field is None:
        raise NotFoundError(f"field {entity_type}.{key} not defined")
    val = sess.execute(
        select(models.CustomFieldValue).where(
            models.CustomFieldValue.def_id == field.id,
            models.CustomFieldValue.entity_id == entity_id,
        )
    ).scalar_one_or_none()
    if val is None:
        raise NotFoundError(f"no value for {entity_type}.{key} on {entity_id}")
    sess.delete(val)
    sess.flush()
    audit_mod.record(
        sess, team_id=team_id, action="custom_field.unset",
        entity_type=entity_type, entity_id=entity_id, actor=actor,
        metadata={"key": key},
    )


__all__ = [
    "VALID_ENTITIES", "VALID_KINDS",
    "define_field", "list_fields", "delete_field",
    "set_value", "list_values", "delete_value", "get_typed_value",
]
