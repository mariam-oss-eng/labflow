"""Wiki / knowledge base (v0.11).

Slug-addressed markdown pages with immutable revision history. Each
``upsert`` writes a new :class:`labflow.models.WikiRevision` row and
re-materialises smart links from the body so backlinks stay current.

Soft delete: pages are flagged ``deleted=True`` rather than removed, so
incoming links from old revisions of other pages still resolve.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit as audit_mod, links as links_mod, models
from .errors import NotFoundError, ValidationError
from .time_utils import now_utc


def _summary_from_body(body: str, n: int = 200) -> str:
    """First non-empty line of the body, truncated to ``n`` chars."""
    for line in (body or "").splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s[:n]
    return ""


def upsert_page(
    sess: Session, *, team_id: int, title: str, body: str,
    slug: str | None = None, summary: str | None = None,
    actor: str = "system",
) -> models.WikiPage:
    if not title or not title.strip():
        raise ValidationError("title is required")
    s = (slug or links_mod.slugify(title)).strip().lower()
    if not s:
        raise ValidationError("slug must be non-empty")
    page = sess.execute(
        select(models.WikiPage).where(
            models.WikiPage.team_id == team_id,
            models.WikiPage.slug == s,
        )
    ).scalar_one_or_none()
    now = now_utc()
    is_create = page is None
    if page is None:
        page = models.WikiPage(
            team_id=team_id, slug=s, title=title.strip(),
            summary=(summary or _summary_from_body(body))[:512],
            body=body or "",
            created_at=now, updated_at=now,
        )
        sess.add(page)
        sess.flush()
    else:
        page.title = title.strip()
        page.summary = (summary or _summary_from_body(body))[:512]
        page.body = body or ""
        page.updated_at = now
        page.deleted = False
        sess.flush()

    # Always write a revision row (even for first save) so the history
    # is complete from the start.
    rev = models.WikiRevision(
        page_id=page.id, title=page.title, body=page.body, author=actor,
        created_at=now,
    )
    sess.add(rev)
    sess.flush()
    page.current_revision_id = rev.id
    sess.flush()

    links_mod.replace_links_for(
        sess, team_id=team_id, source_type="wiki", source_id=page.id,
        text=page.body,
    )
    audit_mod.record(
        sess, team_id=team_id, actor=actor,
        action="wiki.page.created" if is_create else "wiki.page.updated",
        entity_type="wiki", entity_id=page.id,
        metadata={"slug": page.slug, "revision_id": rev.id},
    )
    return page


def get_page(sess: Session, *, team_id: int, slug: str,
             include_deleted: bool = False) -> models.WikiPage:
    page = sess.execute(
        select(models.WikiPage).where(
            models.WikiPage.team_id == team_id,
            models.WikiPage.slug == slug,
        )
    ).scalar_one_or_none()
    if page is None:
        raise NotFoundError(f"wiki page {slug!r} not found")
    if page.deleted and not include_deleted:
        raise NotFoundError(f"wiki page {slug!r} not found")
    return page


def list_pages(sess: Session, *, team_id: int,
               include_deleted: bool = False) -> list[models.WikiPage]:
    q = select(models.WikiPage).where(models.WikiPage.team_id == team_id)
    if not include_deleted:
        q = q.where(models.WikiPage.deleted.is_(False))
    return list(sess.execute(q.order_by(models.WikiPage.slug.asc())).scalars())


def revisions_for(
    sess: Session, *, team_id: int, slug: str,
) -> list[models.WikiRevision]:
    page = get_page(sess, team_id=team_id, slug=slug)
    return list(sess.execute(
        select(models.WikiRevision)
        .where(models.WikiRevision.page_id == page.id)
        .order_by(models.WikiRevision.id.desc())
    ).scalars())


def soft_delete(sess: Session, *, team_id: int, slug: str,
                actor: str = "system") -> None:
    page = get_page(sess, team_id=team_id, slug=slug)
    page.deleted = True
    page.updated_at = now_utc()
    sess.flush()
    audit_mod.record(
        sess, team_id=team_id, actor=actor, action="wiki.page.deleted",
        entity_type="wiki", entity_id=page.id, metadata={"slug": slug},
    )


def search(sess: Session, *, team_id: int, q: str,
           limit: int = 20) -> list[models.WikiPage]:
    """Naive case-insensitive substring search over title + body."""
    if not q:
        return []
    needle = f"%{q.lower()}%"
    rows = sess.execute(
        select(models.WikiPage).where(
            models.WikiPage.team_id == team_id,
            models.WikiPage.deleted.is_(False),
        )
    ).scalars().all()
    hits = [
        p for p in rows
        if needle.strip("%") in (p.title or "").lower()
        or needle.strip("%") in (p.body or "").lower()
        or needle.strip("%") in (p.summary or "").lower()
    ]
    return hits[: max(1, min(limit, 100))]
