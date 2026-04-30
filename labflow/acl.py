"""Resource-level ACLs and share links (v0.8).

The team-wide RBAC roles (``viewer / member / admin``) decide what an API key
can do; this module decides *which rows* it can do those things to. Together
they form a two-tier authorization model:

  1. Role check (existing) — can this key perform this *kind* of action?
  2. ACL check (new)       — is this key allowed to touch *this resource*?

ACLs only restrict — when no ACL row exists for a resource, the role check
alone applies (back-compat with v0.7 behaviour). When *any* ACL row exists
for a resource, only listed keys (or wildcard rows) are allowed.

Share links are a separate concern: they grant read-only access to a single
resource for an unauthenticated client (e.g., to share a meeting with a
non-team contributor). Tokens are stored hashed; passcodes (if set) are
checked in constant time.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit as audit_mod, models
from .errors import AuthError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from .time_utils import now_utc


# --------------------------------------------------------------------------- ACLs

VALID_ENTITIES = frozenset({"meeting", "decision", "task"})
VALID_PERMS = ("read", "write")


def _check_entity(sess: Session, *, team_id: int,
                  entity_type: str, entity_id: int) -> None:
    if entity_type not in VALID_ENTITIES:
        raise ValidationError(f"unsupported entity_type: {entity_type!r}")
    table = {"meeting": models.Meeting,
             "decision": models.Decision,
             "task": models.Task}[entity_type]
    obj = sess.get(table, entity_id)
    if obj is None or getattr(obj, "team_id", None) != team_id:
        raise NotFoundError(f"{entity_type} {entity_id} not found in team")


def grant(
    sess: Session, *, team_id: int, entity_type: str, entity_id: int,
    api_key_id: int | None, permission: str = "read",
    actor: str = "system",
) -> models.ResourceAcl:
    if permission not in VALID_PERMS:
        raise ValidationError(f"permission must be one of {VALID_PERMS}")
    _check_entity(sess, team_id=team_id, entity_type=entity_type, entity_id=entity_id)
    existing = sess.execute(
        select(models.ResourceAcl).where(
            models.ResourceAcl.team_id == team_id,
            models.ResourceAcl.entity_type == entity_type,
            models.ResourceAcl.entity_id == entity_id,
            models.ResourceAcl.api_key_id == api_key_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.permission = permission
        return existing
    row = models.ResourceAcl(
        team_id=team_id, entity_type=entity_type, entity_id=entity_id,
        api_key_id=api_key_id, permission=permission,
    )
    sess.add(row)
    sess.flush()
    audit_mod.record(sess, team_id=team_id, actor=actor,
                     action="acl.grant", entity_type=entity_type,
                     entity_id=entity_id,
                     metadata={"api_key_id": api_key_id,
                               "permission": permission})
    return row


def revoke(sess: Session, *, team_id: int, acl_id: int,
           actor: str = "system") -> None:
    row = sess.get(models.ResourceAcl, acl_id)
    if row is None or row.team_id != team_id:
        raise NotFoundError("acl not found")
    sess.delete(row)
    audit_mod.record(sess, team_id=team_id, actor=actor,
                     action="acl.revoke", entity_type=row.entity_type,
                     entity_id=row.entity_id,
                     metadata={"api_key_id": row.api_key_id})


def list_acls(sess: Session, *, team_id: int,
              entity_type: str | None = None,
              entity_id: int | None = None) -> list[models.ResourceAcl]:
    q = select(models.ResourceAcl).where(models.ResourceAcl.team_id == team_id)
    if entity_type is not None:
        q = q.where(models.ResourceAcl.entity_type == entity_type)
    if entity_id is not None:
        q = q.where(models.ResourceAcl.entity_id == entity_id)
    return list(sess.execute(q.order_by(models.ResourceAcl.created_at)).scalars())


def is_allowed(
    sess: Session, *, team_id: int, entity_type: str, entity_id: int,
    api_key_id: int | None, permission: str = "read",
) -> bool:
    """Return True if ``api_key_id`` may perform ``permission`` on the resource.

    Resource has no ACLs => allowed (delegate to role check).
    Resource has any ACL => allowed iff a matching row (key-specific OR
    wildcard) grants ``permission`` or stronger ("write" implies "read").
    """
    rows = list_acls(sess, team_id=team_id,
                     entity_type=entity_type, entity_id=entity_id)
    if not rows:
        return True
    needed_rank = {"read": 1, "write": 2}[permission]
    for r in rows:
        if r.api_key_id is not None and r.api_key_id != api_key_id:
            continue
        granted = {"read": 1, "write": 2}.get(r.permission, 0)
        if granted >= needed_rank:
            return True
    return False


def assert_allowed(
    sess: Session, *, team_id: int, entity_type: str, entity_id: int,
    api_key_id: int | None, permission: str = "read",
) -> None:
    if not is_allowed(sess, team_id=team_id, entity_type=entity_type,
                      entity_id=entity_id, api_key_id=api_key_id,
                      permission=permission):
        raise ForbiddenError(
            f"resource ACL denies {permission} on {entity_type} {entity_id}"
        )


# --------------------------------------------------------------------------- share links

_SHARE_PREFIX = "lfsl_"
_DEFAULT_TTL_HOURS = 24 * 7
_MAX_TTL_HOURS = 24 * 90


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def create_share_link(
    sess: Session, *, team_id: int, entity_type: str, entity_id: int,
    ttl_hours: int = _DEFAULT_TTL_HOURS, passcode: str | None = None,
    created_by_key_id: int | None = None,
    actor: str = "system",
) -> tuple[models.ShareLink, str]:
    """Mint a new share link. Returns ``(row, plaintext_token)``.

    The plaintext token is shown to the operator exactly once; only its
    SHA-256 is persisted.
    """
    if entity_type not in VALID_ENTITIES:
        raise ValidationError(f"unsupported entity_type: {entity_type!r}")
    if ttl_hours <= 0 or ttl_hours > _MAX_TTL_HOURS:
        raise ValidationError(f"ttl_hours must be 0<x<={_MAX_TTL_HOURS}")
    _check_entity(sess, team_id=team_id, entity_type=entity_type, entity_id=entity_id)
    plaintext = _SHARE_PREFIX + secrets.token_urlsafe(32)
    row = models.ShareLink(
        team_id=team_id, entity_type=entity_type, entity_id=entity_id,
        token_hash=_hash(plaintext),
        passcode_hash=_hash(passcode) if passcode else None,
        expires_at=now_utc().replace(tzinfo=None) + timedelta(hours=ttl_hours),
        created_by_key_id=created_by_key_id,
    )
    sess.add(row)
    sess.flush()
    audit_mod.record(sess, team_id=team_id, actor=actor,
                     action="share.create", entity_type=entity_type,
                     entity_id=entity_id,
                     metadata={"share_id": row.id,
                               "ttl_hours": ttl_hours,
                               "has_passcode": bool(passcode)})
    return row, plaintext


def revoke_share(sess: Session, *, team_id: int, share_id: int,
                 actor: str = "system") -> None:
    row = sess.get(models.ShareLink, share_id)
    if row is None or row.team_id != team_id:
        raise NotFoundError("share link not found")
    row.revoked_at = now_utc().replace(tzinfo=None)
    audit_mod.record(sess, team_id=team_id, actor=actor,
                     action="share.revoke", entity_type=row.entity_type,
                     entity_id=row.entity_id,
                     metadata={"share_id": share_id})


def resolve(
    sess: Session, *, token: str, passcode: str | None = None,
) -> models.ShareLink:
    """Look up a share link by plaintext token, validating freshness + passcode.

    Raises :class:`AuthError` on any failure (expired, revoked, wrong
    passcode, no such token) — never reveals which.
    """
    if not token or not token.startswith(_SHARE_PREFIX):
        raise AuthError("invalid share token")
    row = sess.execute(
        select(models.ShareLink).where(models.ShareLink.token_hash == _hash(token))
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        raise AuthError("invalid share token")
    if row.expires_at is not None and row.expires_at < now_utc().replace(tzinfo=None):
        raise AuthError("invalid share token")
    if row.passcode_hash is not None:
        if passcode is None or not hmac.compare_digest(_hash(passcode), row.passcode_hash):
            raise AuthError("invalid share token")
    return row
