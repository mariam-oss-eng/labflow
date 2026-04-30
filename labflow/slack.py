"""Slack-compatible incoming webhook notifier (v0.8).

Posts a Slack ``blocks`` payload to a configured webhook URL. We use the
stdlib ``urllib`` (no extra dependency) and reuse the same HMAC pattern as
:mod:`labflow.webhooks` for the optional ``X-LabFlow-Signature`` header so
recipients can verify the message originated from this LabFlow instance.

This module is a *renderer* — the actual delivery is delegated to a small
worker so notification posting never blocks an API request.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("labflow.slack")

_USER_AGENT = "labflow-slack/0.8"
_TIMEOUT_S = 5.0


@dataclass
class SlackMessage:
    text: str
    blocks: list[dict[str, Any]]


def render_digest(weekly: dict) -> SlackMessage:
    """Render the output of :func:`labflow.digest.render_weekly` as Slack blocks.

    Falls back to a plain-text fallback in ``text`` so notifications still
    look ok in clients that strip blocks.
    """
    counts = weekly.get("counts", {})
    text = (
        f":bar_chart: *LabFlow weekly digest* — "
        f"{counts.get('meetings', 0)} meetings · "
        f"{counts.get('decisions', 0)} decisions · "
        f"{counts.get('tasks_opened', 0)} tasks opened · "
        f"{counts.get('tasks_closed', 0)} closed"
    )
    blocks: list[dict[str, Any]] = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "LabFlow weekly digest"}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": text}},
    ]
    open_tasks = weekly.get("top_open_tasks", [])
    if open_tasks:
        lines = [f"• {t.get('title', '?')}" for t in open_tasks[:5]]
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": "*Top open tasks*\n" + "\n".join(lines)},
        })
    return SlackMessage(text=text, blocks=blocks)


def post_message(url: str, message: SlackMessage,
                 *, signing_secret: str | None = None) -> bool:
    """POST a Slack-shaped JSON payload. Returns True on 2xx."""
    payload = json.dumps({"text": message.text, "blocks": message.blocks}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
    }
    if signing_secret:
        sig = hmac.new(signing_secret.encode("utf-8"),
                       payload, hashlib.sha256).hexdigest()
        headers["X-LabFlow-Signature"] = "sha256=" + sig
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        log.warning("slack post failed status=%s body=%s", e.code, e.read()[:200])
        return False
    except (urllib.error.URLError, TimeoutError) as e:
        log.warning("slack post network error: %s", e)
        return False
