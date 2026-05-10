"""Smart-list change subscriptions (v0.14).

Subscribe a webhook URL to a saved smart list. A periodic sweeper runs
each subscribed list, computes a SHA-256 digest of the resulting task
IDs, and emits one event **only if the digest changed** since the last
fire. This makes the endpoint cheap and idempotent — a sweep with no
list changes is a no-op.

Delivery is decoupled from detection: :func:`sweep` returns the list of
changed subscriptions (each with its target URL + secret + payload), so
the caller can hand them to whatever transport it wants. Reusing the
existing :mod:`labflow.webhooks` ``emit`` infrastructure would couple
this to the broadcast-to-all-subs model, which isn't what we need here.
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit as audit_mod, models, smart_lists as sl_mod
from .errors import NotFoundError, ValidationError
from .time_utils import now_utc


def _digest_for_ids(ids: list[int]) -> str:
    blob = json.dumps(sorted(int(i) for i in ids), separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def subscribe(
    sess: Session, *, team_id: int, smart_list_slug: str,
    webhook_url: str, secret: str | None = None, actor: str = "system",
) -> models.SmartListSubscription:
    sl = sl_mod.get(sess, team_id=team_id, slug=smart_list_slug)
    url = (webhook_url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValidationError("webhook_url must be an http(s) URL")
    existing = sess.execute(
        select(models.SmartListSubscription).where(
            models.SmartListSubscription.smart_list_id == sl.id,
            models.SmartListSubscription.webhook_url == url,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    sub = models.SmartListSubscription(
        team_id=team_id, smart_list_id=sl.id,
        webhook_url=url, secret=(secret or None),
    )
    sess.add(sub)
    sess.flush()
    audit_mod.record(
        sess, team_id=team_id, action="smart_list.subscribed",
        entity_type="smart_list_subscription", entity_id=sub.id, actor=actor,
        metadata={"smart_list_slug": sl.slug, "webhook_url": url},
    )
    return sub


def list_for(
    sess: Session, *, team_id: int, smart_list_slug: str | None = None,
) -> list[models.SmartListSubscription]:
    q = select(models.SmartListSubscription).where(
        models.SmartListSubscription.team_id == team_id
    )
    if smart_list_slug is not None:
        sl = sl_mod.get(sess, team_id=team_id, slug=smart_list_slug)
        q = q.where(models.SmartListSubscription.smart_list_id == sl.id)
    return list(sess.execute(
        q.order_by(models.SmartListSubscription.id.asc())
    ).scalars().all())


def unsubscribe(
    sess: Session, *, team_id: int, sub_id: int, actor: str = "system",
) -> None:
    sub = sess.get(models.SmartListSubscription, sub_id)
    if sub is None or sub.team_id != team_id:
        raise NotFoundError(f"subscription {sub_id} not found")
    sess.delete(sub)
    audit_mod.record(
        sess, team_id=team_id, action="smart_list.unsubscribed",
        entity_type="smart_list_subscription", entity_id=sub_id, actor=actor,
    )


def sweep(
    sess: Session, *, team_id: int | None = None,
    limit_per_list: int = 200,
) -> list[dict]:
    """Run every active subscription. Returns one event per subscription
    that *changed* (skipped subscriptions are not in the result).

    Each event carries the target ``webhook_url`` + ``secret`` so the
    caller (a worker, a CRON job, a one-shot ``/api/admin/...`` route)
    can deliver it via whatever transport it likes.
    """
    q = select(models.SmartListSubscription)
    if team_id is not None:
        q = q.where(models.SmartListSubscription.team_id == team_id)
    subs = list(sess.execute(q).scalars().all())

    fired: list[dict] = []
    for sub in subs:
        sl = sess.get(models.SmartList, sub.smart_list_id)
        if sl is None:
            continue
        tasks = sl_mod.run(sess, team_id=sub.team_id, slug=sl.slug,
                           limit=limit_per_list)
        ids = [t.id for t in tasks]
        digest = _digest_for_ids(ids)
        if digest == sub.last_digest:
            continue
        sub.last_digest = digest
        sub.last_fired_at = now_utc().replace(tzinfo=None)
        audit_mod.record(
            sess, team_id=sub.team_id, action="smart_list.fired",
            entity_type="smart_list_subscription", entity_id=sub.id,
            metadata={"smart_list_slug": sl.slug,
                      "task_count": len(ids), "digest": digest},
        )
        fired.append({
            "subscription_id": sub.id,
            "smart_list_slug": sl.slug,
            "webhook_url": sub.webhook_url,
            "secret": sub.secret,
            "task_count": len(ids),
            "digest": digest,
            "task_ids": ids,
        })
    return fired
