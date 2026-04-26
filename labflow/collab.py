"""Comments + reactions (v0.6).

Threaded discussion is the social layer over the typed graph: decisions
and tasks accrue context that doesn't fit in the original transcript
(rationale debates, follow-ups, links to artifacts, "+1" reactions).

Design notes
------------
* Generic over entity type via ``(entity_type, entity_id)`` so we don't
  need a separate table per kind.
* ``parent_id`` makes replies form a tree without recursive CTEs — the API
  ships flat rows and the client is expected to nest them by ``parent_id``.
* Each write emits an audit row and a typed SSE event so other tabs see
  the activity in real time.
* Reactions are uniquely keyed on actor so a single user toggling 👍 twice
  is a remove-then-add, never a duplicate.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import audit as audit_mod
from . import models, sse as sse_mod
from .errors import NotFoundError, ValidationError
from .time_utils import now_utc

VALID_ENTITIES: frozenset[str] = frozenset({"decision", "task"})
VALID_EMOJIS: frozenset[str] = frozenset({
    "👍", "👎", "🎉", "❤️", "🚀", "👀", "🤔", "✅",
})


def _check_entity(sess: Session, *, team_id: int, entity_type: str, entity_id: int) -> None:
    if entity_type not in VALID_ENTITIES:
        raise ValidationError(f"unsupported entity_type: {entity_type!r}")
    table = {"decision": models.Decision, "task": models.Task}[entity_type]
    row = sess.get(table, entity_id)
    if row is None or getattr(row, "team_id", None) != team_id:
        raise NotFoundError(f"{entity_type} not found")


# --------------------------------------------------------------------- comments
def add_comment(
    sess: Session,
    *,
    team_id: int,
    entity_type: str,
    entity_id: int,
    body: str,
    actor: str = "system",
    actor_key_id: int | None = None,
    parent_id: int | None = None,
) -> models.Comment:
    body = (body or "").strip()
    if not body:
        raise ValidationError("comment body is empty")
    if len(body) > 8_000:
        raise ValidationError("comment body too long (>8000 chars)")
    _check_entity(sess, team_id=team_id, entity_type=entity_type, entity_id=entity_id)
    if parent_id is not None:
        parent = sess.get(models.Comment, parent_id)
        if parent is None or parent.team_id != team_id:
            raise NotFoundError("parent comment not found")
        if parent.entity_type != entity_type or parent.entity_id != entity_id:
            raise ValidationError("parent comment belongs to a different entity")
    c = models.Comment(
        team_id=team_id,
        entity_type=entity_type,
        entity_id=entity_id,
        parent_id=parent_id,
        actor=actor,
        actor_key_id=actor_key_id,
        body=body,
    )
    sess.add(c)
    sess.flush()
    audit_mod.record(
        sess, team_id=team_id, action="comment.added",
        entity_type="comment", entity_id=c.id, actor=actor,
        metadata={"target": f"{entity_type}:{entity_id}", "parent_id": parent_id},
    )
    sse_mod.hub().publish(team_id, "comment.added", {
        "comment_id": c.id, "entity_type": entity_type,
        "entity_id": entity_id, "actor": actor,
    })
    return c


def list_comments(
    sess: Session, *, team_id: int, entity_type: str, entity_id: int,
) -> list[models.Comment]:
    if entity_type not in VALID_ENTITIES:
        raise ValidationError(f"unsupported entity_type: {entity_type!r}")
    return list(
        sess.execute(
            select(models.Comment)
            .where(
                models.Comment.team_id == team_id,
                models.Comment.entity_type == entity_type,
                models.Comment.entity_id == entity_id,
                models.Comment.deleted_at.is_(None),
            )
            .order_by(models.Comment.created_at, models.Comment.id)
        ).scalars()
    )


def thread_comments(comments: Iterable[models.Comment]) -> list[dict]:
    """Nest a flat list of comments by ``parent_id`` into a forest."""
    by_parent: dict[int | None, list[dict]] = defaultdict(list)
    by_id: dict[int, dict] = {}
    for c in comments:
        d = {
            "id": c.id, "actor": c.actor, "body": c.body,
            "parent_id": c.parent_id,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "edited_at": c.edited_at.isoformat() if c.edited_at else None,
            "replies": [],
        }
        by_id[c.id] = d
        by_parent[c.parent_id].append(d)
    for parent_id, children in by_parent.items():
        if parent_id is not None and parent_id in by_id:
            by_id[parent_id]["replies"] = children
    return by_parent[None]


def soft_delete_comment(
    sess: Session, *, team_id: int, comment_id: int, actor: str = "system",
) -> models.Comment:
    c = sess.get(models.Comment, comment_id)
    if c is None or c.team_id != team_id:
        raise NotFoundError("comment not found")
    if c.deleted_at is None:
        c.deleted_at = now_utc().replace(tzinfo=None)
        c.body = "[deleted]"
        sess.flush()
        audit_mod.record(
            sess, team_id=team_id, action="comment.deleted",
            entity_type="comment", entity_id=c.id, actor=actor,
        )
    return c


# -------------------------------------------------------------------- reactions
def react(
    sess: Session,
    *,
    team_id: int,
    entity_type: str,
    entity_id: int,
    emoji: str,
    actor: str,
    actor_key_id: int | None = None,
) -> tuple[models.Reaction | None, bool]:
    """Toggle a reaction. Returns ``(row | None, added)``."""
    if emoji not in VALID_EMOJIS:
        raise ValidationError(f"unsupported emoji: {emoji!r}")
    _check_entity(sess, team_id=team_id, entity_type=entity_type, entity_id=entity_id)
    existing = sess.execute(
        select(models.Reaction).where(
            models.Reaction.team_id == team_id,
            models.Reaction.entity_type == entity_type,
            models.Reaction.entity_id == entity_id,
            models.Reaction.emoji == emoji,
            models.Reaction.actor == actor,
        )
    ).scalar_one_or_none()
    if existing is not None:
        sess.delete(existing)
        sess.flush()
        return None, False
    r = models.Reaction(
        team_id=team_id, entity_type=entity_type, entity_id=entity_id,
        emoji=emoji, actor=actor, actor_key_id=actor_key_id,
    )
    sess.add(r)
    sess.flush()
    return r, True


def reaction_counts(
    sess: Session, *, team_id: int, entity_type: str, entity_id: int,
) -> dict[str, int]:
    rows = sess.execute(
        select(models.Reaction.emoji)
        .where(
            models.Reaction.team_id == team_id,
            models.Reaction.entity_type == entity_type,
            models.Reaction.entity_id == entity_id,
        )
    ).scalars()
    counts: dict[str, int] = {}
    for emoji in rows:
        counts[emoji] = counts.get(emoji, 0) + 1
    return counts
