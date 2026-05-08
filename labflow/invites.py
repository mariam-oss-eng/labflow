"""Guest invites + federated sharing (v0.13).

Workflow:

1. ``create_invite`` — an admin invites an external email and lists the
   ``(entity_type, entity_id)`` pairs the guest may see. A random
   one-time token is generated; only its SHA-256 hash is stored.
2. ``accept_invite`` — the guest exchanges the plaintext token for a
   freshly minted :class:`labflow.models.ApiKey` (role / scopes copied
   from the invite). One :class:`labflow.models.ResourceAcl` row is
   inserted per ACL entry so the guest's effective view is exactly the
   declared scope.

Tokens never appear in URLs in the audit log; only the hash and the
invite id are recorded.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit as audit_mod, models
from .errors import NotFoundError, ValidationError
from .time_utils import now_utc

VALID_ROLES = frozenset({"viewer", "member", "admin"})
VALID_ENTITY_TYPES = frozenset({
    "meeting", "task", "decision", "experiment", "wiki",
})


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validate_acl(entries: Any) -> list[dict[str, Any]]:
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise ValidationError("acl_entries must be a list")
    out: list[dict[str, Any]] = []
    for e in entries:
        if not isinstance(e, dict):
            raise ValidationError("each acl entry must be an object")
        et = e.get("entity_type")
        eid = e.get("entity_id")
        perm = e.get("permission", "read")
        if et not in VALID_ENTITY_TYPES:
            raise ValidationError(
                f"entity_type {et!r} not in {sorted(VALID_ENTITY_TYPES)}"
            )
        if not isinstance(eid, int) or eid <= 0:
            raise ValidationError("entity_id must be a positive int")
        if perm not in {"read", "write"}:
            raise ValidationError("permission must be 'read' or 'write'")
        out.append({"entity_type": et, "entity_id": eid, "permission": perm})
    return out


def create_invite(
    sess: Session, *, team_id: int, email: str,
    role: str = "viewer", scopes: str | None = None,
    acl_entries: list[dict[str, Any]] | None = None,
    ttl_hours: int = 168,  # 7 days
    created_by_key_id: int | None = None,
    actor: str = "system",
) -> tuple[models.Invite, str]:
    """Create a pending invite. Returns ``(invite, plaintext_token)``.

    The plaintext token is shown to the operator exactly once — re-fetching
    the invite later only yields the hash.
    """
    if role not in VALID_ROLES:
        raise ValidationError(f"role must be one of {sorted(VALID_ROLES)}")
    if not (email or "").strip():
        raise ValidationError("email is required")
    entries = _validate_acl(acl_entries)
    token = secrets.token_urlsafe(32)
    expires_at = (now_utc() + timedelta(hours=max(1, ttl_hours))).replace(tzinfo=None)
    inv = models.Invite(
        team_id=team_id,
        email=email.strip().lower(),
        token_hash=_hash(token),
        role=role,
        scopes=scopes,
        acl_entries_json=json.dumps(entries) if entries else None,
        status="pending",
        created_by_key_id=created_by_key_id,
        expires_at=expires_at,
    )
    sess.add(inv)
    sess.flush()
    audit_mod.record(
        sess, team_id=team_id, action="invite.created",
        entity_type="invite", entity_id=inv.id, actor=actor,
        metadata={"email": inv.email, "role": role, "acl_count": len(entries)},
    )
    return inv, token


def list_invites(
    sess: Session, *, team_id: int, status: str | None = None,
) -> list[models.Invite]:
    q = select(models.Invite).where(models.Invite.team_id == team_id)
    if status is not None:
        q = q.where(models.Invite.status == status)
    q = q.order_by(models.Invite.created_at.desc())
    return list(sess.execute(q).scalars().all())


def revoke_invite(
    sess: Session, *, team_id: int, invite_id: int, actor: str = "system",
) -> None:
    inv = sess.get(models.Invite, invite_id)
    if inv is None or inv.team_id != team_id:
        raise NotFoundError(f"invite #{invite_id} not found")
    if inv.status == "accepted":
        raise ValidationError("cannot revoke an accepted invite")
    inv.status = "revoked"
    sess.flush()
    audit_mod.record(
        sess, team_id=team_id, action="invite.revoked",
        entity_type="invite", entity_id=inv.id, actor=actor,
    )


def accept_invite(
    sess: Session, *, token: str, mint_key,
    actor: str | None = None,
) -> tuple[models.Invite, models.ApiKey, str]:
    """Accept ``token`` and mint a guest API key.

    ``mint_key(sess, team_id, name, scopes)`` is injected so this module
    doesn't have to import :mod:`labflow.auth` (avoiding a cycle). It
    must return ``(api_key_row, plaintext_key)``.

    Returns ``(invite, api_key_row, plaintext_key)``.
    """
    h = _hash(token)
    inv = sess.execute(
        select(models.Invite).where(models.Invite.token_hash == h)
    ).scalar_one_or_none()
    if inv is None:
        raise NotFoundError("invite not found")
    if inv.status != "pending":
        raise ValidationError(f"invite is {inv.status}")
    now = now_utc().replace(tzinfo=None)
    if inv.expires_at and inv.expires_at < now:
        inv.status = "revoked"
        sess.flush()
        raise ValidationError("invite expired")

    api_key, plaintext = mint_key(
        sess,
        team_id=inv.team_id,
        name=f"guest:{inv.email}",
        scopes=inv.scopes,
    )

    # Materialise ACL rows (one per entry) so existing ACL checks apply.
    acl_entries: list[dict[str, Any]] = (
        json.loads(inv.acl_entries_json) if inv.acl_entries_json else []
    )
    for e in acl_entries:
        sess.add(models.ResourceAcl(
            team_id=inv.team_id,
            entity_type=e["entity_type"],
            entity_id=e["entity_id"],
            api_key_id=api_key.id,
            permission=e["permission"],
        ))

    inv.status = "accepted"
    inv.accepted_key_id = api_key.id
    inv.accepted_at = now
    sess.flush()

    audit_mod.record(
        sess, team_id=inv.team_id, action="invite.accepted",
        entity_type="invite", entity_id=inv.id,
        actor=actor or f"guest:{inv.email}",
        metadata={"api_key_id": api_key.id, "acl_count": len(acl_entries)},
    )
    return inv, api_key, plaintext
