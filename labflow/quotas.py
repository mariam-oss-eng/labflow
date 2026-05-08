"""Per-API-key daily request quotas (v0.12).

A two-table design:

* :class:`labflow.models.ApiKeyQuota` — admin-set per-key daily limit.
* :class:`labflow.models.ApiKeyUsage` — the rolling counter, keyed by
  ``(api_key_id, day)`` where ``day`` is a ``YYYY-MM-DD`` UTC string.

The dependency :func:`enforce` increments the counter for the calling
key. If a quota is set *and* the new count exceeds it, a 429 is raised
*before* the route handler runs. Keys without a quota row are unlimited
(back-compat — quotas are opt-in).
"""
from __future__ import annotations

from typing import Iterable

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import audit as audit_mod, models
from .errors import LabFlowError
from .time_utils import now_utc


class QuotaExceededError(LabFlowError):
    """Raised when the calling API key exceeds its daily quota."""

    status_code = 429
    code = "quota_exceeded"


# ---------------------------------------------------------------------------
# Quota CRUD
# ---------------------------------------------------------------------------
def set_quota(
    sess: Session, *, team_id: int, api_key_id: int, daily_limit: int,
    actor: str = "system",
) -> models.ApiKeyQuota:
    if daily_limit < 0:
        raise LabFlowError("daily_limit must be >= 0")
    row = sess.execute(
        select(models.ApiKeyQuota).where(
            models.ApiKeyQuota.api_key_id == api_key_id
        )
    ).scalar_one_or_none()
    if row is None:
        row = models.ApiKeyQuota(
            team_id=team_id, api_key_id=api_key_id,
            daily_limit=daily_limit,
        )
        sess.add(row)
    else:
        row.daily_limit = daily_limit
        row.updated_at = now_utc().replace(tzinfo=None)
    sess.flush()
    audit_mod.record(
        sess, team_id=team_id, action="quota.set",
        entity_type="api_key", entity_id=api_key_id, actor=actor,
        metadata={"daily_limit": daily_limit},
    )
    return row


def list_quotas(sess: Session, *, team_id: int) -> list[models.ApiKeyQuota]:
    return list(sess.execute(
        select(models.ApiKeyQuota)
        .where(models.ApiKeyQuota.team_id == team_id)
        .order_by(models.ApiKeyQuota.api_key_id.asc())
    ).scalars().all())


def delete_quota(
    sess: Session, *, team_id: int, api_key_id: int, actor: str = "system",
) -> None:
    row = sess.execute(
        select(models.ApiKeyQuota).where(
            models.ApiKeyQuota.api_key_id == api_key_id,
            models.ApiKeyQuota.team_id == team_id,
        )
    ).scalar_one_or_none()
    if row is None:
        return
    sess.delete(row)
    audit_mod.record(
        sess, team_id=team_id, action="quota.deleted",
        entity_type="api_key", entity_id=api_key_id, actor=actor,
    )


# ---------------------------------------------------------------------------
# Counter helpers
# ---------------------------------------------------------------------------
def _today() -> str:
    return now_utc().strftime("%Y-%m-%d")


def _get_or_create_usage(
    sess: Session, *, team_id: int, api_key_id: int, day: str,
) -> models.ApiKeyUsage:
    row = sess.execute(
        select(models.ApiKeyUsage).where(
            models.ApiKeyUsage.api_key_id == api_key_id,
            models.ApiKeyUsage.day == day,
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    row = models.ApiKeyUsage(
        team_id=team_id, api_key_id=api_key_id, day=day, count=0,
    )
    sess.add(row)
    try:
        sess.flush()
    except IntegrityError:
        # Another request created it concurrently — fetch the winner.
        sess.rollback()
        row = sess.execute(
            select(models.ApiKeyUsage).where(
                models.ApiKeyUsage.api_key_id == api_key_id,
                models.ApiKeyUsage.day == day,
            )
        ).scalar_one()
    return row


def increment_and_check(
    sess: Session, *, team_id: int, api_key_id: int, day: str | None = None,
) -> tuple[int, int | None]:
    """Atomic-ish increment. Returns ``(new_count, limit_or_None)``.

    Raises :class:`QuotaExceededError` if a limit exists and is exceeded.
    """
    d = day or _today()
    quota = sess.execute(
        select(models.ApiKeyQuota).where(
            models.ApiKeyQuota.api_key_id == api_key_id
        )
    ).scalar_one_or_none()
    limit = quota.daily_limit if quota else None

    row = _get_or_create_usage(sess, team_id=team_id, api_key_id=api_key_id, day=d)
    row.count += 1
    sess.flush()
    if limit is not None and row.count > limit:
        raise QuotaExceededError(
            f"API key {api_key_id} exceeded its daily quota of {limit} "
            f"(used {row.count})"
        )
    return row.count, limit


def usage_summary(
    sess: Session, *, team_id: int, day: str | None = None,
) -> list[dict]:
    """Return per-key usage rows for ``day`` (today by default)."""
    d = day or _today()
    rows = sess.execute(
        select(models.ApiKeyUsage)
        .where(
            models.ApiKeyUsage.team_id == team_id,
            models.ApiKeyUsage.day == d,
        )
        .order_by(models.ApiKeyUsage.count.desc())
    ).scalars().all()
    quotas: dict[int, int] = {
        q.api_key_id: q.daily_limit
        for q in list_quotas(sess, team_id=team_id)
    }
    return [
        {
            "api_key_id": r.api_key_id,
            "day": r.day,
            "count": r.count,
            "limit": quotas.get(r.api_key_id),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------
def enforce(request: Request) -> None:
    """Dependency that increments and checks the quota for the calling key.

    Skipped silently when the request has no resolved ``api_key_id``
    (e.g. anonymous routes, share links, or test fixtures that haven't
    minted a key).
    """
    api_key_id = getattr(request.state, "api_key_id", None)
    team_id = getattr(request.state, "team_id", None)
    if api_key_id is None or team_id is None:
        return
    # Lazy import to avoid a top-level circular with main / db.
    from .db import get_session_factory
    SessionLocal = get_session_factory()
    with SessionLocal() as sess:
        try:
            increment_and_check(sess, team_id=team_id, api_key_id=api_key_id)
            sess.commit()
        except QuotaExceededError:
            sess.commit()  # commit the increment so audit/usage stays accurate
            raise


# Helper so callers (e.g. seed scripts) and tests can inspect the
# enforced exceptions without importing `errors`.
EXCEPTIONS = (QuotaExceededError,)


__all__ = [
    "QuotaExceededError",
    "set_quota",
    "list_quotas",
    "delete_quota",
    "increment_and_check",
    "usage_summary",
    "enforce",
]
