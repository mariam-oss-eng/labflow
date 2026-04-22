"""Persistence layer for extraction results.

Bridges the stateless extraction pipeline to SQLAlchemy models, including:

  * upserting owners by handle
  * resolving owner_handle / depends_on_titles into FK relationships
  * preserving cross-meeting decision continuity (decisions with the same
    statement are linked via ``superseded_by_id`` so we maintain a graph
    over time rather than duplicating).
"""
from __future__ import annotations

from typing import Dict, List

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .schemas import ExtractionResult


def upsert_owners(sess: Session, owners) -> Dict[str, models.Owner]:
    by_handle: Dict[str, models.Owner] = {}
    for o in owners:
        existing = sess.execute(
            select(models.Owner).where(models.Owner.handle == o.handle)
        ).scalar_one_or_none()
        if existing is None:
            existing = models.Owner(handle=o.handle, display_name=o.display_name)
            sess.add(existing)
            sess.flush()
        by_handle[o.handle] = existing
    return by_handle


def persist_extraction(
    sess: Session, meeting: models.Meeting, result: ExtractionResult
) -> None:
    """Persist an :class:`ExtractionResult` against ``meeting``.

    Existing decisions/tasks/experiments/assumptions/blockers on the meeting
    are cleared so this function is idempotent under re-extraction.
    """
    meeting.decisions.clear()
    meeting.tasks.clear()
    meeting.experiments.clear()
    meeting.assumptions.clear()
    meeting.blockers.clear()
    sess.flush()

    owners_by_handle = upsert_owners(sess, result.owners)

    # Decisions — link to prior decisions with the same statement.
    for d in result.decisions:
        prior = sess.execute(
            select(models.Decision).where(models.Decision.statement == d.statement)
        ).scalars().all()
        new_decision = models.Decision(
            meeting=meeting,
            statement=d.statement,
            rationale=d.rationale,
            confidence=d.confidence,
        )
        sess.add(new_decision)
        sess.flush()
        for old in prior:
            if old.id != new_decision.id and old.superseded_by_id is None:
                old.superseded_by_id = new_decision.id

    # Tasks — first pass to create rows, second pass to link dependencies.
    title_to_task: Dict[str, models.Task] = {}
    for t in result.tasks:
        owner = owners_by_handle.get(t.owner_handle) if t.owner_handle else None
        task = models.Task(
            meeting=meeting,
            title=t.title,
            description=t.description,
            owner=owner,
            due_date=t.due_date,
            kind=t.kind,
            uncertainty=t.uncertainty,
            confidence=t.confidence,
            source_span=t.source_span,
        )
        sess.add(task)
        sess.flush()
        title_to_task[t.title] = task

    for t in result.tasks:
        task = title_to_task[t.title]
        for dep_title in t.depends_on_titles:
            dep = title_to_task.get(dep_title)
            if dep is not None and dep is not task:
                task.depends_on.append(dep)

    for e in result.experiments:
        owner = owners_by_handle.get(e.owner_handle) if e.owner_handle else None
        sess.add(
            models.Experiment(
                meeting=meeting,
                name=e.name,
                hypothesis=e.hypothesis,
                method=e.method,
                metrics=",".join(e.metrics) if e.metrics else None,
                dataset=e.dataset,
                owner=owner,
            )
        )

    for a in result.assumptions:
        sess.add(
            models.Assumption(
                meeting=meeting, statement=a.statement, risk=a.risk
            )
        )

    for b in result.blockers:
        blocked_task = (
            title_to_task.get(b.blocked_task_title) if b.blocked_task_title else None
        )
        sess.add(
            models.Blocker(
                meeting=meeting,
                description=b.description,
                blocked_task_id=blocked_task.id if blocked_task else None,
            )
        )

    sess.flush()


def list_open_tasks(sess: Session) -> List[models.Task]:
    return list(
        sess.execute(
            select(models.Task).where(models.Task.status != "done").order_by(models.Task.id)
        ).scalars()
    )
