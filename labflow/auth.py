"""Authentication and authorization.

Auth model: a workspace (``Team``) holds many ``ApiKey`` rows. Every
data row (``Meeting``, …) belongs to exactly one team. Requests authenticate
by sending the API key as either ``Authorization: Bearer <key>`` or the
``X-LabFlow-Key`` header.

API keys are stored *hashed* (SHA-256) — the plaintext key is shown to the
user exactly once, at creation time.

When ``LABFLOW_AUTH_ENABLED=false`` (default for local dev) the system runs
in "single-team" mode: every request resolves to the bootstrap team without
needing a header. This keeps the existing local quickstart working.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from typing import Optional

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .config import get_settings
from .errors import AuthError, ForbiddenError
from .time_utils import now_utc

log = logging.getLogger("labflow.auth")

# Plaintext keys are prefixed with `lfk_` so they're easy to grep / revoke
# accidentally-leaked secrets in code.
_KEY_PREFIX = "lfk_"


def hash_api_key(plaintext: str) -> str:
    """Return the storage hash for an API key. Plaintext is never persisted."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    """Mint a new plaintext API key (caller must hash before persisting)."""
    return _KEY_PREFIX + secrets.token_urlsafe(32)


def _extract_key(authorization: str | None, x_labflow_key: str | None) -> str | None:
    if x_labflow_key:
        return x_labflow_key.strip()
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            return value.strip()
    return None


def ensure_bootstrap_team(db: Session) -> models.Team:
    """Create the bootstrap team + API key on first boot if needed.

    Idempotent. Returns the bootstrap team.
    """
    settings = get_settings()
    team = db.execute(
        select(models.Team).where(models.Team.slug == settings.bootstrap_team)
    ).scalar_one_or_none()
    if team is None:
        team = models.Team(
            slug=settings.bootstrap_team,
            name=settings.bootstrap_team.title(),
            created_at=now_utc(),
        )
        db.add(team)
        db.flush()
    if settings.bootstrap_api_key:
        key_hash = hash_api_key(settings.bootstrap_api_key)
        existing = db.execute(
            select(models.ApiKey).where(models.ApiKey.key_hash == key_hash)
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                models.ApiKey(
                    team_id=team.id,
                    name="bootstrap",
                    key_hash=key_hash,
                    created_at=now_utc(),
                )
            )
            db.flush()
    db.commit()
    return team


def get_current_team(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_labflow_key: Optional[str] = Header(default=None, alias="X-LabFlow-Key"),
    db: Session = Depends(lambda: None),  # placeholder, real dep injected at app build
) -> models.Team:
    """FastAPI dependency that resolves the current ``Team``.

    Bound to a real DB session by :func:`labflow.main.create_app` because
    avoiding a circular import requires deferring the dependency wiring.
    """
    raise RuntimeError("get_current_team must be re-bound by create_app()")


def build_team_dependency(db_dep):
    """Return a dependency that uses the given ``db_dep`` to resolve the team.

    Implemented this way so the auth module doesn't import the FastAPI app
    factory (which would create a cycle).
    """
    settings = get_settings()

    def _dep(
        request: Request,
        authorization: Optional[str] = Header(default=None),
        x_labflow_key: Optional[str] = Header(default=None, alias="X-LabFlow-Key"),
        db: Session = Depends(db_dep),
    ) -> models.Team:
        # Single-team mode: no header required, fall back to bootstrap team.
        if not settings.auth_enabled:
            team = db.execute(
                select(models.Team).where(models.Team.slug == settings.bootstrap_team)
            ).scalar_one_or_none()
            if team is None:
                team = ensure_bootstrap_team(db)
            request.state.team_id = team.id
            return team

        plaintext = _extract_key(authorization, x_labflow_key)
        if not plaintext:
            raise AuthError("missing API key (set Authorization: Bearer <key>)")
        key = db.execute(
            select(models.ApiKey).where(
                models.ApiKey.key_hash == hash_api_key(plaintext),
                models.ApiKey.revoked_at.is_(None),
            )
        ).scalar_one_or_none()
        if key is None:
            raise AuthError("invalid or revoked API key")
        team = db.get(models.Team, key.team_id)
        if team is None or team.disabled:
            raise ForbiddenError("team disabled")
        # Update last_used_at lazily; ignore errors so a logging bug never
        # blocks a request.
        try:
            key.last_used_at = now_utc()
        except Exception:
            pass
        request.state.team_id = team.id
        request.state.api_key_id = key.id
        return team

    return _dep


def assert_team_owns(obj, team: models.Team) -> None:
    """Raise 403 if ``obj.team_id`` doesn't match ``team.id``."""
    obj_team_id = getattr(obj, "team_id", None)
    if obj_team_id is None or obj_team_id != team.id:
        raise ForbiddenError("resource belongs to another team")
