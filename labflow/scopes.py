"""API-key scopes (v0.8).

Adds OAuth-style fine-grained capability tokens on top of the existing RBAC
roles. A scope is a string in the set::

    read           — read any resource the role allows
    write          — create/modify any resource the role allows
    admin          — full administrative access (mirrors the admin role)
    webhook:emit   — may register / fire outbound webhooks
    plugin:install — may install / enable plugins (v0.9)

A key with ``scopes IS NULL`` (default for legacy keys) has *all* scopes —
this preserves v0.7 behaviour. New keys created via the v0.8 admin API
must declare scopes explicitly.
"""
from __future__ import annotations

from typing import Iterable

from fastapi import Depends, Request

from . import models
from .errors import ForbiddenError


VALID_SCOPES = frozenset({
    "read", "write", "admin", "webhook:emit", "plugin:install",
})


def parse_scopes(raw: str | None) -> set[str] | None:
    """Parse a comma-separated scope CSV. ``None`` => all scopes."""
    if raw is None:
        return None
    parts = {p.strip() for p in raw.split(",") if p.strip()}
    unknown = parts - VALID_SCOPES
    if unknown:
        raise ValueError(f"unknown scopes: {sorted(unknown)}")
    return parts


def serialise_scopes(scopes: Iterable[str] | None) -> str | None:
    if scopes is None:
        return None
    return ",".join(sorted(set(scopes)))


def has_scope(key: models.ApiKey | None, scope: str) -> bool:
    if key is None:
        return True  # bootstrap / single-team mode
    if key.scopes is None or key.scopes == "":
        return True  # all scopes (legacy)
    granted = parse_scopes(key.scopes) or set()
    if "admin" in granted:
        return True
    return scope in granted


def require_scope(scope: str):
    """FastAPI dependency: 403 if the current key lacks ``scope``.

    Looks up the key from ``request.state.api_key_id`` (set by the team
    dependency). When auth is disabled this is a no-op so local dev keeps
    working without scope plumbing.
    """
    if scope not in VALID_SCOPES:
        raise ValueError(f"unknown scope {scope!r}")

    def _dep(request: Request) -> None:
        key_id = getattr(request.state, "api_key_id", None)
        if key_id is None:
            return  # single-team / bootstrap
        # Re-fetch the API key out of the request-scoped session so the
        # check uses fresh DB state. We avoid pulling in get_db here to
        # keep this module dependency-free of FastAPI app wiring.
        from .db import get_session_factory
        with get_session_factory()() as sess:
            key = sess.get(models.ApiKey, key_id)
            if not has_scope(key, scope):
                raise ForbiddenError(f"missing scope {scope!r}")

    return _dep
