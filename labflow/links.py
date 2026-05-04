"""Smart entity-link parser (v0.11).

Materialises three syntactic patterns from any text into typed
:class:`labflow.models.EntityLink` rows:

* ``#task-123`` → reference to an existing task
* ``[[Page Name]]`` → wiki link (creates a *placeholder* page row if
  none exists yet, so backlinks survive the order in which pages are
  written)
* ``@handle`` → mention of an :class:`labflow.models.Owner`

Parsing is deliberately conservative — only ASCII-safe identifiers
match — so the regexes are both fast and free of pathological
backtracking. The slugifier matches the rest of the codebase
(``slugify`` in :mod:`labflow.sprints`) by lowercasing and replacing
non-alphanumerics with hyphens.
"""
from __future__ import annotations

import re
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .time_utils import now_utc

# Anchors prevent partial matches inside larger words: "##task-123" or
# "X#task-123" will not match (the leading char must be start-of-string
# or whitespace/punctuation). All three patterns use bounded character
# classes — no nested quantifiers, no catastrophic backtracking.
_TASK_RE = re.compile(r"(?:^|(?<=\s))#task-(\d{1,9})\b")
_WIKI_RE = re.compile(r"\[\[([^\[\]\n]{1,128})\]\]")
_MENTION_RE = re.compile(r"(?:^|(?<=\s))@([A-Za-z][A-Za-z0-9_\-]{0,63})\b")


def slugify(s: str) -> str:
    out = re.sub(r"[^A-Za-z0-9]+", "-", (s or "").strip().lower())
    return out.strip("-") or "untitled"


def parse(text: str) -> dict[str, list[str]]:
    """Pure parser, no DB. Returns the raw matches, deduplicated.

    Useful for tests and for the search highlighter.
    """
    if not text:
        return {"task_ids": [], "wikilinks": [], "mentions": []}
    return {
        "task_ids": list({m.group(1) for m in _TASK_RE.finditer(text)}),
        "wikilinks": list({m.group(1).strip() for m in _WIKI_RE.finditer(text)}),
        "mentions": list({m.group(1) for m in _MENTION_RE.finditer(text)}),
    }


def _ensure_link(
    sess: Session, *, team_id: int, source_type: str, source_id: int,
    target_type: str, target_id: int, kind: str,
) -> bool:
    """Insert a link row if it doesn't already exist; return True if new."""
    existing = sess.execute(
        select(models.EntityLink.id).where(
            models.EntityLink.team_id == team_id,
            models.EntityLink.source_type == source_type,
            models.EntityLink.source_id == source_id,
            models.EntityLink.target_type == target_type,
            models.EntityLink.target_id == target_id,
            models.EntityLink.kind == kind,
        )
    ).first()
    if existing is not None:
        return False
    sess.add(models.EntityLink(
        team_id=team_id, source_type=source_type, source_id=source_id,
        target_type=target_type, target_id=target_id, kind=kind,
    ))
    return True


def materialise(
    sess: Session, *, team_id: int, source_type: str, source_id: int,
    text: str,
) -> dict[str, int]:
    """Resolve patterns in ``text`` to entity ids and persist links.

    Strategy:
      * ``#task-N`` → only links if a task with that id exists *for the
        same team* (cross-tenant references are silently dropped).
      * ``[[Page]]`` → resolves to the wiki page with the matching slug;
        if missing, a *placeholder* page is created with empty body so a
        forward link survives until the page is authored.
      * ``@handle`` → resolves to an existing :class:`Owner` by handle;
        unknown handles are dropped (we don't auto-create owners — that
        belongs to extraction).

    Returns a counts dict ``{"task": int, "wiki": int, "mention": int}``.
    """
    parsed = parse(text)
    counts = {"task": 0, "wiki": 0, "mention": 0}

    # Tasks --------------------------------------------------------
    for raw in parsed["task_ids"]:
        try:
            tid = int(raw)
        except ValueError:
            continue
        task = sess.get(models.Task, tid)
        if task is None or task.team_id != team_id:
            continue
        if _ensure_link(
            sess, team_id=team_id, source_type=source_type, source_id=source_id,
            target_type="task", target_id=task.id, kind="ref",
        ):
            counts["task"] += 1

    # Wiki links ---------------------------------------------------
    for title in parsed["wikilinks"]:
        slug = slugify(title)
        page = sess.execute(
            select(models.WikiPage).where(
                models.WikiPage.team_id == team_id,
                models.WikiPage.slug == slug,
            )
        ).scalar_one_or_none()
        if page is None:
            now = now_utc()
            page = models.WikiPage(
                team_id=team_id, slug=slug, title=title.strip(),
                summary=None, body="", created_at=now, updated_at=now,
            )
            sess.add(page)
            sess.flush()
        # Don't link a page to itself.
        if source_type == "wiki" and page.id == source_id:
            continue
        if _ensure_link(
            sess, team_id=team_id, source_type=source_type, source_id=source_id,
            target_type="wiki", target_id=page.id, kind="wikilink",
        ):
            counts["wiki"] += 1

    # Mentions -----------------------------------------------------
    for handle in parsed["mentions"]:
        owner = sess.execute(
            select(models.Owner).where(
                models.Owner.team_id == team_id,
                models.Owner.handle == handle,
            )
        ).scalar_one_or_none()
        if owner is None:
            continue
        if _ensure_link(
            sess, team_id=team_id, source_type=source_type, source_id=source_id,
            target_type="owner", target_id=owner.id, kind="mention",
        ):
            counts["mention"] += 1

    sess.flush()
    return counts


def replace_links_for(
    sess: Session, *, team_id: int, source_type: str, source_id: int,
    text: str,
) -> dict[str, int]:
    """Drop existing outbound links from this source, then re-materialise.

    Used when a wiki page or comment body is edited.
    """
    sess.execute(
        models.EntityLink.__table__.delete().where(
            models.EntityLink.team_id == team_id,
            models.EntityLink.source_type == source_type,
            models.EntityLink.source_id == source_id,
        )
    )
    return materialise(
        sess, team_id=team_id, source_type=source_type,
        source_id=source_id, text=text,
    )


def backlinks_for(
    sess: Session, *, team_id: int, target_type: str, target_id: int,
) -> list[dict]:
    """All inbound links pointing at ``(target_type, target_id)``."""
    rows = sess.execute(
        select(models.EntityLink).where(
            models.EntityLink.team_id == team_id,
            models.EntityLink.target_type == target_type,
            models.EntityLink.target_id == target_id,
        ).order_by(models.EntityLink.id.asc())
    ).scalars().all()
    return [
        {"source_type": r.source_type, "source_id": r.source_id,
         "kind": r.kind, "created_at": r.created_at.isoformat()}
        for r in rows
    ]
