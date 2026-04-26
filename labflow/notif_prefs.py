"""Per-user notification preferences (v0.6).

Lazy-creates a :class:`NotificationPref` row on first read so the API can
treat the table as if it always had a row for every key. Mute filters
let a user opt out of specific event types (e.g. ``task.closed``)
without losing their digest cadence.
"""
from __future__ import annotations

import json
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .errors import ValidationError
from .time_utils import now_utc

VALID_CADENCE = frozenset({"off", "daily", "weekly"})


def get_or_create(
    sess: Session, *, team_id: int, api_key_id: int,
) -> models.NotificationPref:
    row = sess.execute(
        select(models.NotificationPref).where(
            models.NotificationPref.api_key_id == api_key_id
        )
    ).scalar_one_or_none()
    if row is None:
        row = models.NotificationPref(team_id=team_id, api_key_id=api_key_id)
        sess.add(row)
        sess.flush()
    return row


def update(
    sess: Session,
    *,
    team_id: int,
    api_key_id: int,
    digest_cadence: str | None = None,
    email: str | None = None,
    muted_events: Iterable[str] | None = None,
) -> models.NotificationPref:
    row = get_or_create(sess, team_id=team_id, api_key_id=api_key_id)
    if digest_cadence is not None:
        if digest_cadence not in VALID_CADENCE:
            raise ValidationError(
                f"digest_cadence must be one of {sorted(VALID_CADENCE)}"
            )
        row.digest_cadence = digest_cadence
    if email is not None:
        row.email = email or None
    if muted_events is not None:
        items = sorted({str(e) for e in muted_events if str(e).strip()})
        row.muted_events = json.dumps(items) if items else None
    row.updated_at = now_utc().replace(tzinfo=None)
    sess.flush()
    return row


def to_dict(row: models.NotificationPref) -> dict:
    return {
        "digest_cadence": row.digest_cadence,
        "email": row.email,
        "muted_events": json.loads(row.muted_events) if row.muted_events else [],
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def is_muted(row: models.NotificationPref, event: str) -> bool:
    if not row.muted_events:
        return False
    try:
        muted = json.loads(row.muted_events)
    except Exception:
        return False
    return event in muted
