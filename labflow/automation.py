"""Declarative automation rules engine (v0.10).

A rule is a JSON document::

    {
      "name": "review-handoff",
      "trigger_event": "task.transition",
      "condition": {"to_state": "in_review"},
      "actions": [
        {"kind": "tag", "params": {"tag": "needs-review"}},
        {"kind": "notify", "params": {"message": "Task moved to review"}},
        {"kind": "webhook", "params": {"event": "task.review_requested"}}
      ]
    }

* ``trigger_event`` matches the audit-log ``action`` field (e.g.
  ``task.transition``, ``meeting.finalized``).
* ``condition`` is a small DSL: a flat dict whose keys must equal the
  same key in the event metadata. This intentionally avoids an
  expression evaluator — operator-supplied JSON is a serious attack
  surface and a one-line "all keys match" check has zero CVE budget.
* ``actions`` are dispatched in declared order. Built-in kinds:
    - ``notify``: emits an SSE event ``automation.notify`` with the
      message.
    - ``webhook``: emits a custom event name via the standard
      :mod:`labflow.webhooks` machinery.
    - ``tag``: writes a metadata-only audit row tagging the entity.

The dispatcher is **synchronous** and **best-effort**: failures of
individual actions are logged but never raised, so a misconfigured rule
cannot block the underlying state change.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models, sse as sse_mod
from .errors import ValidationError
from .time_utils import now_utc

log = logging.getLogger("labflow.automation")

VALID_ACTION_KINDS = {"notify", "webhook", "tag"}


def _validate_definition(trigger_event: str, condition: dict | None,
                         actions: list[dict]) -> None:
    if not trigger_event or not isinstance(trigger_event, str):
        raise ValidationError("trigger_event is required")
    if condition is not None and not isinstance(condition, dict):
        raise ValidationError("condition must be an object or null")
    if not isinstance(actions, list) or not actions:
        raise ValidationError("actions must be a non-empty list")
    for a in actions:
        if not isinstance(a, dict) or "kind" not in a:
            raise ValidationError("each action needs a 'kind'")
        if a["kind"] not in VALID_ACTION_KINDS:
            raise ValidationError(
                f"unknown action kind '{a['kind']}'; "
                f"valid: {sorted(VALID_ACTION_KINDS)}"
            )
        if "params" in a and not isinstance(a["params"], dict):
            raise ValidationError("action.params must be an object")


def create_rule(
    sess: Session, *, team_id: int, name: str, trigger_event: str,
    actions: list[dict], condition: dict | None = None,
    enabled: bool = True,
) -> models.AutomationRule:
    _validate_definition(trigger_event, condition, actions)
    existing = sess.execute(
        select(models.AutomationRule).where(
            models.AutomationRule.team_id == team_id,
            models.AutomationRule.name == name,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.trigger_event = trigger_event
        existing.condition_json = (
            json.dumps(condition, sort_keys=True) if condition else None
        )
        existing.actions_json = json.dumps(actions)
        existing.enabled = enabled
        sess.flush()
        return existing
    rule = models.AutomationRule(
        team_id=team_id, name=name, trigger_event=trigger_event,
        condition_json=(json.dumps(condition, sort_keys=True)
                        if condition else None),
        actions_json=json.dumps(actions), enabled=enabled,
    )
    sess.add(rule)
    sess.flush()
    return rule


def list_rules(sess: Session, *, team_id: int) -> list[models.AutomationRule]:
    return list(
        sess.execute(
            select(models.AutomationRule)
            .where(models.AutomationRule.team_id == team_id)
            .order_by(models.AutomationRule.id.asc())
        ).scalars()
    )


def delete_rule(sess: Session, *, team_id: int, name: str) -> None:
    rule = sess.execute(
        select(models.AutomationRule).where(
            models.AutomationRule.team_id == team_id,
            models.AutomationRule.name == name,
        )
    ).scalar_one_or_none()
    if rule is None:
        return
    sess.delete(rule)
    sess.flush()


def _condition_matches(condition: dict | None, metadata: dict) -> bool:
    if not condition:
        return True
    for k, v in condition.items():
        if metadata.get(k) != v:
            return False
    return True


def dispatch(
    sess: Session, *, team_id: int, event: str, entity_type: str,
    entity_id: int | None, metadata: dict | None,
) -> int:
    """Find and execute matching rules. Returns number of rules fired.

    Called from :func:`labflow.audit.record`. Designed to be cheap when
    no rules exist for a team (single indexed query).
    """
    rules = sess.execute(
        select(models.AutomationRule).where(
            models.AutomationRule.team_id == team_id,
            models.AutomationRule.trigger_event == event,
            models.AutomationRule.enabled.is_(True),
        )
    ).scalars().all()
    if not rules:
        return 0
    fired = 0
    md = metadata or {}
    for rule in rules:
        try:
            cond = (json.loads(rule.condition_json)
                    if rule.condition_json else None)
        except Exception:
            log.warning("automation: bad condition_json on rule %s", rule.id)
            continue
        if not _condition_matches(cond, md):
            continue
        try:
            actions = json.loads(rule.actions_json)
        except Exception:
            log.warning("automation: bad actions_json on rule %s", rule.id)
            continue
        for action in actions:
            try:
                _execute(sess, team_id=team_id, action=action,
                         entity_type=entity_type, entity_id=entity_id,
                         event=event, metadata=md, rule_name=rule.name)
            except Exception as exc:  # noqa: BLE001
                log.warning("automation: action %s on rule %s failed: %s",
                            action.get("kind"), rule.name, exc)
        rule.fires += 1
        rule.last_fired_at = now_utc()
        fired += 1
    sess.flush()
    return fired


def _execute(
    sess: Session, *, team_id: int, action: dict, entity_type: str,
    entity_id: int | None, event: str, metadata: dict, rule_name: str,
) -> None:
    kind = action.get("kind")
    params = action.get("params") or {}
    if kind == "notify":
        sse_mod.hub().publish(team_id, "automation.notify", {
            "rule": rule_name,
            "message": str(params.get("message", "")),
            "entity_type": entity_type, "entity_id": entity_id,
            "trigger": event,
        })
        return
    if kind == "webhook":
        # We import lazily to avoid a circular dependency: automation is
        # called from audit.record which is itself called from many
        # places that webhooks may also call into.
        try:
            from . import webhooks as wh
        except Exception:
            return
        emitted_event = str(params.get("event") or f"automation.{rule_name}")
        if hasattr(wh, "emit"):
            wh.emit(sess, team_id=team_id, event=emitted_event, payload={
                "rule": rule_name, "entity_type": entity_type,
                "entity_id": entity_id, "trigger": event,
                "metadata": metadata,
            })
        return
    if kind == "tag":
        # Tagging writes an audit row via the chained recorder so the
        # hash chain stays continuous. Recursion is bounded: the new row
        # has action ``automation.tag`` which only fires rules subscribed
        # to that exact event, and those would themselves only re-fire
        # if an operator deliberately wrote such a rule.
        from . import audit as _audit
        tag = str(params.get("tag", "tagged"))
        _audit.record(
            sess, team_id=team_id, actor=f"automation:{rule_name}",
            action="automation.tag", entity_type=entity_type,
            entity_id=entity_id,
            metadata={"tag": tag, "trigger": event},
        )
        return
