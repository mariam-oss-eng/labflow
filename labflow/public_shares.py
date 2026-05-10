"""Public, read-only share links (v0.15).

A user with the right role can mint a time-bound, revocable, read-only
URL pointing at one of:

* a decision (``/share/decision/<token>``)
* a task    (``/share/task/<token>``)
* a wiki page (``/share/wiki/<token>``)

The token is a 32-byte random string returned to the creator **exactly
once**. We persist its SHA-256 hash, mirroring the API-key pattern in
:mod:`labflow.auth` (high-entropy machine-generated token → SHA-256 is
the appropriate, fast verification primitive — the
``py/weak-sensitive-data-hashing`` CodeQL rule does not apply, see
``auth.hash_api_key`` for the rationale).

Lookup is constant-time per share: hash the token, then ``WHERE
token_hash = ?``.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit as audit_mod, models
from .errors import ConflictError, NotFoundError, ValidationError
from .time_utils import now_utc

VALID_ENTITIES = frozenset({"decision", "task", "wiki"})
_TOKEN_PREFIX = "lfshare_"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _generate_token() -> str:
    return _TOKEN_PREFIX + secrets.token_urlsafe(32)


@dataclass
class CreatedShare:
    """Returned by :func:`create` — the plaintext token is included
    here exactly once."""

    id: int
    token: str
    entity_type: str
    entity_id: int
    expires_at: object  # datetime


def _validate_entity(entity_type: str, entity_id: int,
                     sess: Session, *, team_id: int) -> None:
    if entity_type not in VALID_ENTITIES:
        raise ValidationError(
            f"entity_type must be one of {sorted(VALID_ENTITIES)}"
        )
    if entity_type == "decision":
        row = sess.get(models.Decision, entity_id)
    elif entity_type == "task":
        row = sess.get(models.Task, entity_id)
    else:  # wiki
        row = sess.get(models.WikiPage, entity_id)
    if row is None or row.team_id != team_id:
        raise NotFoundError(f"{entity_type} {entity_id} not found")


def create(
    sess: Session, *, team_id: int, entity_type: str, entity_id: int,
    ttl_hours: int = 168, actor: str = "system",
) -> CreatedShare:
    """Mint a fresh share. Default TTL is 7 days (168h)."""
    if ttl_hours < 1 or ttl_hours > 24 * 90:
        raise ValidationError("ttl_hours must be in [1, 2160]")
    _validate_entity(entity_type, entity_id, sess, team_id=team_id)
    token = _generate_token()
    expires = now_utc().replace(tzinfo=None) + timedelta(hours=ttl_hours)
    row = models.PublicShare(
        team_id=team_id, entity_type=entity_type, entity_id=entity_id,
        token_hash=_hash(token), created_by=actor, expires_at=expires,
    )
    sess.add(row)
    sess.flush()
    audit_mod.record(
        sess, team_id=team_id, action="share.created",
        entity_type="public_share", entity_id=row.id, actor=actor,
        metadata={"target_type": entity_type, "target_id": entity_id,
                  "expires_at": expires.isoformat()},
    )
    return CreatedShare(
        id=row.id, token=token, entity_type=entity_type,
        entity_id=entity_id, expires_at=expires,
    )


def list_for(
    sess: Session, *, team_id: int,
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
) -> list[models.PublicShare]:
    q = select(models.PublicShare).where(models.PublicShare.team_id == team_id)
    if entity_type is not None:
        q = q.where(models.PublicShare.entity_type == entity_type)
    if entity_id is not None:
        q = q.where(models.PublicShare.entity_id == entity_id)
    return list(sess.execute(
        q.order_by(models.PublicShare.id.desc())
    ).scalars().all())


def revoke(
    sess: Session, *, team_id: int, share_id: int, actor: str = "system",
) -> models.PublicShare:
    row = sess.get(models.PublicShare, share_id)
    if row is None or row.team_id != team_id:
        raise NotFoundError(f"share {share_id} not found")
    if row.revoked_at is not None:
        raise ConflictError("share already revoked")
    row.revoked_at = now_utc().replace(tzinfo=None)
    audit_mod.record(
        sess, team_id=team_id, action="share.revoked",
        entity_type="public_share", entity_id=row.id, actor=actor,
    )
    return row


def resolve(
    sess: Session, *, token: str,
) -> tuple[models.PublicShare, dict]:
    """Look up an unexpired, unrevoked share and return ``(row,
    rendered_entity)``. Increments ``view_count``."""
    if not token or not token.startswith(_TOKEN_PREFIX):
        raise NotFoundError("share not found")
    row = sess.execute(
        select(models.PublicShare).where(
            models.PublicShare.token_hash == _hash(token),
        )
    ).scalar_one_or_none()
    now = now_utc().replace(tzinfo=None)
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        raise NotFoundError("share not found")
    row.view_count = (row.view_count or 0) + 1
    rendered = _render(sess, row)
    return row, rendered


def _render(sess: Session, row: models.PublicShare) -> dict:
    if row.entity_type == "decision":
        d = sess.get(models.Decision, row.entity_id)
        if d is None:
            raise NotFoundError("entity removed")
        return {"type": "decision", "id": d.id,
                "statement": d.statement, "rationale": d.rationale,
                "created_at": d.created_at.isoformat() if d.created_at else None}
    if row.entity_type == "task":
        t = sess.get(models.Task, row.entity_id)
        if t is None:
            raise NotFoundError("entity removed")
        return {"type": "task", "id": t.id, "title": t.title,
                "description": t.description, "status": t.status,
                "state": t.state, "priority": t.priority,
                "due_date": t.due_date.isoformat() if t.due_date else None}
    # wiki
    p = sess.get(models.WikiPage, row.entity_id)
    if p is None:
        raise NotFoundError("entity removed")
    return {"type": "wiki", "id": p.id, "slug": p.slug,
            "title": p.title, "body": p.body}
