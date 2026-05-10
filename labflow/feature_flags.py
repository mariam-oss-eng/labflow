"""Per-team feature flags (v0.14).

A pragmatic small implementation: just a key/value table per team. Reads
go through a one-second LRU-ish cache so a hot route can call
:func:`is_enabled` on every request without round-tripping the DB.

Flags also accept an optional JSON ``payload`` so a flag can carry
parameters (e.g. ``{"variant": "B", "bucket": 0.5}``) without inventing
a separate config system.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit as audit_mod, models
from .errors import NotFoundError, ValidationError
from .time_utils import now_utc

_CACHE: dict[tuple[int, str], tuple[float, bool, Optional[str]]] = {}
_CACHE_TTL = 1.0  # seconds — a short TTL is fine; admins flip flags rarely


def _validate_key(key: str) -> str:
    k = (key or "").strip().lower()
    if not k or len(k) > 80:
        raise ValidationError("flag key must be 1..80 chars")
    if not all(c.isalnum() or c in "._-" for c in k):
        raise ValidationError("flag key may only contain [a-z0-9._-]")
    return k


def _cache_set(team_id: int, key: str, enabled: bool,
               payload_json: Optional[str]) -> None:
    _CACHE[(team_id, key)] = (time.monotonic(), enabled, payload_json)


def _cache_get(team_id: int, key: str
               ) -> Optional[tuple[bool, Optional[str]]]:
    v = _CACHE.get((team_id, key))
    if v is None:
        return None
    ts, enabled, payload = v
    if time.monotonic() - ts > _CACHE_TTL:
        return None
    return enabled, payload


def reset_cache() -> None:
    _CACHE.clear()


def is_enabled(
    sess: Session, *, team_id: int, key: str, default: bool = False,
) -> bool:
    """Return whether flag ``key`` is on for ``team_id``.

    Uses a short-lived process-local cache (TTL ~1s)."""
    k = _validate_key(key)
    cached = _cache_get(team_id, k)
    if cached is not None:
        return cached[0]
    row = sess.execute(
        select(models.FeatureFlag).where(
            models.FeatureFlag.team_id == team_id,
            models.FeatureFlag.key == k,
        )
    ).scalar_one_or_none()
    if row is None:
        _cache_set(team_id, k, default, None)
        return default
    _cache_set(team_id, k, row.enabled, row.payload_json)
    return row.enabled


def payload(
    sess: Session, *, team_id: int, key: str,
) -> Optional[dict[str, Any]]:
    """Return the parsed JSON payload of a flag (or ``None``)."""
    k = _validate_key(key)
    cached = _cache_get(team_id, k)
    if cached is not None:
        raw = cached[1]
    else:
        row = sess.execute(
            select(models.FeatureFlag).where(
                models.FeatureFlag.team_id == team_id,
                models.FeatureFlag.key == k,
            )
        ).scalar_one_or_none()
        if row is None:
            _cache_set(team_id, k, False, None)
            return None
        _cache_set(team_id, k, row.enabled, row.payload_json)
        raw = row.payload_json
    if not raw:
        return None
    try:
        v = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return v if isinstance(v, dict) else None


def upsert(
    sess: Session, *, team_id: int, key: str, enabled: bool,
    payload: Optional[dict[str, Any]] = None, actor: str = "system",
) -> models.FeatureFlag:
    k = _validate_key(key)
    payload_json: Optional[str] = None
    if payload is not None:
        if not isinstance(payload, dict):
            raise ValidationError("payload must be a JSON object")
        payload_json = json.dumps(payload, sort_keys=True)
    row = sess.execute(
        select(models.FeatureFlag).where(
            models.FeatureFlag.team_id == team_id,
            models.FeatureFlag.key == k,
        )
    ).scalar_one_or_none()
    if row is None:
        row = models.FeatureFlag(
            team_id=team_id, key=k, enabled=bool(enabled),
            payload_json=payload_json,
        )
        sess.add(row)
    else:
        row.enabled = bool(enabled)
        row.payload_json = payload_json
        row.updated_at = now_utc().replace(tzinfo=None)
    sess.flush()
    _cache_set(team_id, k, row.enabled, row.payload_json)
    audit_mod.record(
        sess, team_id=team_id, action="feature_flag.upserted",
        entity_type="feature_flag", entity_id=row.id, actor=actor,
        metadata={"key": k, "enabled": row.enabled},
    )
    return row


def list_all(sess: Session, *, team_id: int) -> list[models.FeatureFlag]:
    return list(sess.execute(
        select(models.FeatureFlag)
        .where(models.FeatureFlag.team_id == team_id)
        .order_by(models.FeatureFlag.key.asc())
    ).scalars().all())


def delete(
    sess: Session, *, team_id: int, key: str, actor: str = "system",
) -> None:
    k = _validate_key(key)
    row = sess.execute(
        select(models.FeatureFlag).where(
            models.FeatureFlag.team_id == team_id,
            models.FeatureFlag.key == k,
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"feature flag {key!r} not found")
    sess.delete(row)
    _CACHE.pop((team_id, k), None)
    audit_mod.record(
        sess, team_id=team_id, action="feature_flag.deleted",
        entity_type="feature_flag", entity_id=row.id, actor=actor,
        metadata={"key": k},
    )
