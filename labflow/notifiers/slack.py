"""Slack-formatted webhook payloads (v0.4).

Slack incoming webhooks accept a JSON envelope with ``text`` /
``blocks`` keys. Reusing the existing :mod:`labflow.webhooks` machinery,
we transparently *re-shape* the payload at delivery time when the
subscription URL points at ``hooks.slack.com``. Customers don't have to
configure a separate Slack integration — they just paste a webhook URL.

We deliberately avoid the ``slack-sdk`` dependency: a single JSON POST
is all that's needed for inbound webhooks, and the SDK pulls in four
transitive packages and a logging pipeline we don't want.
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse


def is_slack_url(url: str) -> bool:
    """True iff ``url`` is a Slack incoming-webhook URL.

    Uses ``urlparse`` to compare the *hostname* — a substring check
    such as ``"hooks.slack.com" in url`` would be fooled by attacker-
    controlled paths like ``https://evil.com/hooks.slack.com.fake``.
    """
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host == "hooks.slack.com" or host.endswith(".hooks.slack.com")


def to_slack_payload(event: str, data: dict[str, Any]) -> str:
    """Reshape a LabFlow event into a Slack ``Block Kit`` payload."""
    title, fields = _summarize(event, data)
    blocks: list[dict] = [
        {"type": "header",
         "text": {"type": "plain_text", "text": f"LabFlow · {title}"}},
    ]
    if fields:
        blocks.append({
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*{k}*\n{v}"} for k, v in fields
            ],
        })
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"`event` *{event}*"}],
    })
    return json.dumps({"text": title, "blocks": blocks})


def _summarize(event: str, data: dict[str, Any]) -> tuple[str, list[tuple[str, str]]]:
    if event == "meeting.finalized":
        return (f"Meeting finalized: {data.get('title', 'unknown')}",
                [("Meeting ID", str(data.get("meeting_id", "?")))])
    if event == "meeting.extracted":
        return ("Meeting extracted",
                [("Tasks", str(data.get("tasks", 0))),
                 ("Decisions", str(data.get("decisions", 0)))])
    if event == "task.closed":
        return (f"Task closed: {data.get('title', 'unknown')}",
                [("Task ID", str(data.get("task_id", "?")))])
    if event == "evidence.verified":
        return ("Evidence verified",
                [("Task ID", str(data.get("task_id", "?"))),
                 ("URI", str(data.get("uri", "")))])
    return (event, [(k, str(v)) for k, v in list(data.items())[:8]])
