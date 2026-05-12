"""API key rotation with a grace window (v0.17).

Rotating an API key without rotation support means downtime: clients
have to swap to the new key the moment the old one is revoked.

LabFlow's rotation flow is:

1. ``rotate(old_key_id, grace_hours)`` creates a fresh key, returns its
   plaintext **once**, and stamps ``rotation_grace_until`` on the old
   key. Both keys are valid during the grace window.
2. Operators redeploy clients with the new key.
3. ``sweep_expired()`` (cron-friendly) revokes any key whose
   ``rotation_grace_until`` is in the past.

The auth lookup itself is unchanged — it still filters on
``revoked_at IS NULL`` — so a swept key is rejected exactly like any
other revoked key.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit as audit_mod, auth, models
from .errors import ConflictError, NotFoundError, ValidationError
from .time_utils import now_utc

# Sensible bounds: at least 1h to give clients time to redeploy, at
# most 30 days because beyond that operators should mint a fresh key
# and treat the old one as compromised.
GRACE_MIN_HOURS = 1
GRACE_MAX_HOURS = 24 * 30
GRACE_DEFAULT_HOURS = 24


@dataclass
class RotatedKey:
    """Returned by :func:`rotate` — the plaintext token is here exactly once."""

    new_id: int
    new_token: str
    old_id: int
    grace_until: object   # datetime


def rotate(
    sess: Session, *, team_id: int, old_key_id: int,
    grace_hours: int = GRACE_DEFAULT_HOURS,
    name_suffix: str = "rotated", actor: str = "system",
) -> RotatedKey:
    """Create a successor key for ``old_key_id`` and start the grace window.

    The old key keeps working until ``rotation_grace_until``. After the
    sweeper runs (or after :func:`sweep_expired`), the old key is
    revoked.
    """
    if not (GRACE_MIN_HOURS <= grace_hours <= GRACE_MAX_HOURS):
        raise ValidationError(
            f"grace_hours must be in [{GRACE_MIN_HOURS}, {GRACE_MAX_HOURS}]"
        )
    old = sess.get(models.ApiKey, old_key_id)
    if old is None or old.team_id != team_id:
        raise NotFoundError(f"api key {old_key_id} not found")
    if old.revoked_at is not None:
        raise ConflictError(f"api key {old_key_id} is already revoked")
    if old.rotation_grace_until is not None:
        raise ConflictError(
            f"api key {old_key_id} is already in a rotation grace window"
        )

    plaintext = auth.generate_api_key()
    grace_until = now_utc() + timedelta(hours=grace_hours)

    new_key = models.ApiKey(
        team_id=team_id,
        name=f"{old.name}-{name_suffix}",
        key_hash=auth.hash_api_key(plaintext),
        scopes=old.scopes,
        rotated_from_id=old.id,
    )
    sess.add(new_key)
    old.rotation_grace_until = grace_until
    sess.flush()
    audit_mod.record(
        sess, team_id=team_id, action="api_key.rotate",
        entity_type="api_key", entity_id=new_key.id, actor=actor,
        metadata={"old_id": old.id, "grace_hours": grace_hours},
    )
    return RotatedKey(
        new_id=new_key.id, new_token=plaintext,
        old_id=old.id, grace_until=grace_until,
    )


def sweep_expired(
    sess: Session, *, team_id: Optional[int] = None,
    actor: str = "system",
) -> int:
    """Revoke any key whose grace window has elapsed. Returns the
    number revoked. Safe to run on a cron without arguments."""
    now = now_utc()
    q = (
        select(models.ApiKey).where(
            models.ApiKey.rotation_grace_until.isnot(None),
            models.ApiKey.rotation_grace_until <= now,
            models.ApiKey.revoked_at.is_(None),
        )
    )
    if team_id is not None:
        q = q.where(models.ApiKey.team_id == team_id)
    rows = list(sess.execute(q).scalars())
    for k in rows:
        k.revoked_at = now
        audit_mod.record(
            sess, team_id=k.team_id, action="api_key.rotate.expire",
            entity_type="api_key", entity_id=k.id, actor=actor,
        )
    if rows:
        sess.flush()
    return len(rows)


def cancel_rotation(
    sess: Session, *, team_id: int, old_key_id: int, actor: str = "system",
) -> models.ApiKey:
    """Abort an in-progress rotation: clear the grace window so the
    old key reverts to "indefinitely valid" and the operator can
    re-issue a rotation later. Does NOT revoke the successor key —
    callers should explicitly delete it if they don't intend to use it.
    """
    old = sess.get(models.ApiKey, old_key_id)
    if old is None or old.team_id != team_id:
        raise NotFoundError(f"api key {old_key_id} not found")
    if old.rotation_grace_until is None:
        raise ValidationError(
            f"api key {old_key_id} is not in a rotation grace window"
        )
    old.rotation_grace_until = None
    sess.flush()
    audit_mod.record(
        sess, team_id=team_id, action="api_key.rotate.cancel",
        entity_type="api_key", entity_id=old.id, actor=actor,
    )
    return old


__all__ = [
    "GRACE_MIN_HOURS", "GRACE_MAX_HOURS", "GRACE_DEFAULT_HOURS",
    "RotatedKey", "rotate", "sweep_expired", "cancel_rotation",
]
