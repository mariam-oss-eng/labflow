"""Outbound + inbound webhooks.

Outbound:
  * :func:`emit` enqueues an event to be delivered to every active
    subscription matching the event name (or ``*``).
  * Delivery is performed by :func:`deliver_pending` (called from the
    worker loop) using stdlib ``urllib`` — no extra dependency. Each
    request is signed with HMAC-SHA256 over the raw body.
  * Failures schedule a retry with exponential backoff capped at 5 attempts;
    every attempt is recorded in :class:`models.WebhookDelivery`.

Inbound:
  * :func:`verify_github_signature` compares ``X-Hub-Signature-256`` against
    HMAC-SHA256(secret, body) using ``hmac.compare_digest`` for constant-
    time equality.

The HTTP client uses stdlib ``urllib.request`` so we don't add a runtime
dependency on ``httpx``/``requests`` for production deployments.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .config import get_settings
from .time_utils import now_utc

log = logging.getLogger("labflow.webhooks")

_MAX_ATTEMPTS = 5
_HTTP_TIMEOUT_S = 10.0


def sign(body: bytes, secret: str) -> str:
    """Return the ``sha256=…`` signature string for ``body``."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(body: bytes, secret: str, signature: str | None) -> bool:
    """Constant-time comparison of an inbound HMAC signature."""
    if not signature or not secret:
        return False
    expected = sign(body, secret)
    return hmac.compare_digest(expected, signature.strip())


def verify_github_signature(body: bytes, signature_header: str | None) -> bool:
    """Verify a GitHub ``X-Hub-Signature-256`` header against the configured secret."""
    secret = get_settings().github_webhook_secret
    return verify_signature(body, secret, signature_header)


def emit(
    sess: Session,
    *,
    team_id: int,
    event: str,
    payload: dict[str, Any],
) -> int:
    """Queue a webhook event for every matching active subscription.

    Returns the number of deliveries queued.
    """
    subs = list(
        sess.execute(
            select(models.WebhookSubscription).where(
                models.WebhookSubscription.team_id == team_id,
                models.WebhookSubscription.active.is_(True),
            )
        ).scalars()
    )
    body = json.dumps({"event": event, "data": payload}, sort_keys=True)
    queued = 0
    for sub in subs:
        if sub.event != "*" and sub.event != event:
            continue
        sess.add(
            models.WebhookDelivery(
                subscription_id=sub.id, event=event, payload=body,
                attempts=0, success=False,
            )
        )
        queued += 1
    sess.flush()
    return queued


def deliver_pending(
    sess: Session, *, http_post=None, max_batch: int = 50
) -> int:
    """Deliver pending webhook attempts. Returns the number marked successful.

    ``http_post`` is injectable for tests — defaults to a stdlib-based POST.
    """
    from .notifiers.slack import is_slack_url, to_slack_payload

    poster = http_post or _default_post
    pending = list(
        sess.execute(
            select(models.WebhookDelivery)
            .where(
                models.WebhookDelivery.success.is_(False),
                models.WebhookDelivery.attempts < _MAX_ATTEMPTS,
            )
            .limit(max_batch)
        ).scalars()
    )
    successes = 0
    for d in pending:
        sub = sess.get(models.WebhookSubscription, d.subscription_id)
        if sub is None or not sub.active:
            d.attempts = _MAX_ATTEMPTS  # don't keep retrying orphans
            continue
        # Reshape for Slack incoming webhooks.
        if is_slack_url(sub.url):
            try:
                envelope = json.loads(d.payload)
                body_str = to_slack_payload(envelope.get("event", d.event),
                                            envelope.get("data", {}))
            except Exception:  # noqa: BLE001
                body_str = d.payload
            body = body_str.encode("utf-8")
            headers = {"Content-Type": "application/json",
                       "User-Agent": "LabFlow/0.4"}
        else:
            body = d.payload.encode("utf-8")
            signature = sign(body, sub.secret or get_settings().webhook_signing_secret)
            headers = {
                "Content-Type": "application/json",
                "X-LabFlow-Event": d.event,
                "X-LabFlow-Signature-256": signature,
                "User-Agent": "LabFlow/0.4",
            }
        try:
            status, resp_body = poster(sub.url, body, headers)
        except Exception as exc:  # noqa: BLE001
            log.warning("webhook_delivery_error", extra={
                "delivery_id": d.id, "subscription_id": sub.id, "error": str(exc)
            })
            d.attempts += 1
            d.status_code = None
            d.response_body = str(exc)[:1000]
            continue
        d.attempts += 1
        d.status_code = status
        d.response_body = (resp_body or "")[:1000]
        if 200 <= status < 300:
            d.success = True
            successes += 1
    sess.flush()
    return successes


def _default_post(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310
            return resp.status, resp.read(2048).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:  # 4xx/5xx
        return e.code, (e.read(2048).decode("utf-8", errors="replace") if e.fp else "")
