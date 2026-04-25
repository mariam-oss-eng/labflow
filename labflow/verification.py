"""Evidence-driven completion verification.

The verifier scores a piece of evidence against a task by combining:

  * keyword overlap between the evidence summary/uri and the task title,
  * owner-match bonus when the evidence references the task's owner,
  * kind-specific bonuses (a ``commit`` referencing the task title is a
    much stronger signal than an arbitrary doc link).

When the score crosses a threshold we mark both ``Evidence.verified`` and
the task ``status='done'``. The threshold is conservative on purpose — we
prefer surfacing evidence for human approval over silently auto-closing.
"""
from __future__ import annotations

import re
from typing import Iterable

from sqlalchemy.orm import Session

from . import models
from .time_utils import now_utc

_TOKEN = re.compile(r"[a-z0-9]{3,}")
_VERIFY_THRESHOLD = 0.5

KIND_BONUS = {
    "commit": 0.20,
    "eval": 0.20,
    "checklist": 0.15,
    "doc": 0.05,
    "link": 0.0,
}


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall((text or "").lower()))


def score_evidence(task: models.Task, evidence: models.Evidence) -> float:
    title_tokens = _tokens(task.title) | _tokens(task.description or "")
    ev_tokens = _tokens(evidence.summary or "") | _tokens(evidence.uri or "")
    if not title_tokens or not ev_tokens:
        overlap = 0.0
    else:
        overlap = len(title_tokens & ev_tokens) / max(len(title_tokens), 1)

    score = overlap + KIND_BONUS.get(evidence.kind, 0.0)

    if task.owner is not None:
        owner_handle = task.owner.handle.lower()
        if owner_handle and owner_handle in (evidence.summary or "").lower():
            score += 0.15
        if owner_handle and owner_handle in (evidence.uri or "").lower():
            score += 0.10

    return min(1.0, max(0.0, score))


def verify_evidence(sess: Session, evidence: models.Evidence) -> bool:
    """Score the evidence and, if convincing, mark the task done.

    Returns True if the evidence verified the task, False otherwise.
    """
    task = evidence.task
    score = score_evidence(task, evidence)
    evidence.score = score
    if score >= _VERIFY_THRESHOLD:
        evidence.verified = True
        if task.status != "done":
            task.status = "done"
            task.closed_at = now_utc()
        sess.flush()
        return True
    sess.flush()
    return False


def reverify_all(sess: Session, tasks: Iterable[models.Task] | None = None) -> int:
    """Re-score evidence for ``tasks`` (defaults to all). Returns # closed."""
    if tasks is None:
        tasks = sess.query(models.Task).all()
    closed = 0
    for task in tasks:
        for ev in task.evidence:
            if verify_evidence(sess, ev):
                closed += 1
                break
    return closed
