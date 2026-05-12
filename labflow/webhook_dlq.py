"""Webhook dead-letter queue (v0.17).

Built on the existing :class:`labflow.models.WebhookDelivery` rows.

A delivery enters the DLQ when its retry counter reaches the cap
(:data:`labflow.webhooks._MAX_ATTEMPTS`) without success: the sweeper
:func:`mark_dead` flips ``dead_lettered_at`` to the current timestamp.

DLQ operations:

* :func:`list_dead`        — paginated listing of dead-lettered rows
* :func:`replay`           — clear the dead-letter flag and reset
                             ``attempts`` so the regular delivery loop
                             picks it up again
* :func:`discard`          — mark as definitively abandoned (does not
                             delete; the row remains for audit)

Every transition emits an audit event so postmortems can reconstruct
who reanimated/discarded what.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit as audit_mod, models
from .errors import NotFoundError, ValidationError
from .time_utils import now_utc
from .webhooks import _MAX_ATTEMPTS


def mark_dead(sess: Session, *, team_id: Optional[int] = None) -> int:
    """Flip ``dead_lettered_at`` on every delivery that has exhausted
    its retries without success. Returns the number marked.

    Scopes by team if ``team_id`` is provided (joins through the
    subscription); otherwise sweeps the whole table — useful for the
    operator-level cron.
    """
    q = (
        select(models.WebhookDelivery)
        .where(
            models.WebhookDelivery.success.is_(False),
            models.WebhookDelivery.attempts >= _MAX_ATTEMPTS,
            models.WebhookDelivery.dead_lettered_at.is_(None),
        )
    )
    if team_id is not None:
        q = q.join(
            models.WebhookSubscription,
            models.WebhookSubscription.id
            == models.WebhookDelivery.subscription_id,
        ).where(models.WebhookSubscription.team_id == team_id)
    rows = list(sess.execute(q).scalars())
    now = now_utc()
    for r in rows:
        r.dead_lettered_at = now
    if rows:
        sess.flush()
    return len(rows)


def list_dead(
    sess: Session, *, team_id: int, limit: int = 100, offset: int = 0,
) -> list[models.WebhookDelivery]:
    return list(sess.execute(
        select(models.WebhookDelivery)
        .join(
            models.WebhookSubscription,
            models.WebhookSubscription.id
            == models.WebhookDelivery.subscription_id,
        )
        .where(
            models.WebhookSubscription.team_id == team_id,
            models.WebhookDelivery.dead_lettered_at.isnot(None),
        )
        .order_by(models.WebhookDelivery.id.desc())
        .offset(max(offset, 0))
        .limit(min(max(limit, 1), 500))
    ).scalars())


def _load_team_delivery(
    sess: Session, *, team_id: int, delivery_id: int,
) -> models.WebhookDelivery:
    d = sess.get(models.WebhookDelivery, delivery_id)
    if d is None:
        raise NotFoundError(f"delivery {delivery_id} not found")
    sub = sess.get(models.WebhookSubscription, d.subscription_id)
    if sub is None or sub.team_id != team_id:
        raise NotFoundError(f"delivery {delivery_id} not found")
    return d


def replay(
    sess: Session, *, team_id: int, delivery_id: int, actor: str = "system",
) -> models.WebhookDelivery:
    d = _load_team_delivery(sess, team_id=team_id, delivery_id=delivery_id)
    if d.dead_lettered_at is None:
        raise ValidationError(
            f"delivery {delivery_id} is not dead-lettered"
        )
    d.dead_lettered_at = None
    d.attempts = 0
    d.success = False
    d.status_code = None
    d.response_body = None
    sess.flush()
    audit_mod.record(
        sess, team_id=team_id, action="webhook_dlq.replay",
        entity_type="webhook_delivery", entity_id=d.id, actor=actor,
        metadata={"event": d.event},
    )
    return d


def discard(
    sess: Session, *, team_id: int, delivery_id: int, actor: str = "system",
) -> models.WebhookDelivery:
    d = _load_team_delivery(sess, team_id=team_id, delivery_id=delivery_id)
    if d.dead_lettered_at is None:
        raise ValidationError(
            f"delivery {delivery_id} is not dead-lettered"
        )
    # Mark as a permanent failure: success stays False, attempts stays
    # at cap, dead_lettered_at stays set. We just record the audit so
    # operators can see who acknowledged it.
    audit_mod.record(
        sess, team_id=team_id, action="webhook_dlq.discard",
        entity_type="webhook_delivery", entity_id=d.id, actor=actor,
        metadata={"event": d.event},
    )
    return d


def stats(sess: Session, *, team_id: int) -> dict[str, int]:
    """Return ``{"dead": N, "pending": M, "success_recent": K}``."""
    base = (
        select(models.WebhookDelivery)
        .join(
            models.WebhookSubscription,
            models.WebhookSubscription.id
            == models.WebhookDelivery.subscription_id,
        )
        .where(models.WebhookSubscription.team_id == team_id)
    )
    rows = list(sess.execute(base).scalars())
    dead = sum(1 for r in rows if r.dead_lettered_at is not None)
    pending = sum(1 for r in rows
                  if not r.success and r.dead_lettered_at is None)
    success = sum(1 for r in rows if r.success)
    return {"dead": dead, "pending": pending, "success": success}


__all__ = ["mark_dead", "list_dead", "replay", "discard", "stats"]
