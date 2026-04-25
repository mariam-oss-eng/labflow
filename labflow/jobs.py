"""In-process background job queue.

Production deployments run a separate ``worker`` container (see
``docker-compose.yml``) that calls :func:`run_forever`. The same handler
registry is also used by the API to enqueue jobs synchronously when the
caller passes ``async_=False``.

Why not Celery / RQ / Arq? — Adding a broker (Redis / RabbitMQ) is a real
operational burden. The job rates LabFlow needs to handle (transcript
extraction, webhook fan-out, audit retention) fit comfortably within a
DB-backed queue running on a single Postgres instance with ``SELECT … FOR
UPDATE SKIP LOCKED`` or, on SQLite, a simple status flag. We keep the
abstraction so a Celery adapter can be slotted in if scale ever demands it.
"""
from __future__ import annotations

import json
import logging
import time
import traceback
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .db import get_session_factory
from .time_utils import now_utc

log = logging.getLogger("labflow.jobs")

# kind -> (handler, description)
_HANDLERS: dict[str, Callable[[Session, models.Job, dict[str, Any]], dict[str, Any] | None]] = {}


def register(kind: str):
    """Decorator: register ``fn`` as the handler for ``kind`` jobs."""
    def deco(fn):
        _HANDLERS[kind] = fn
        return fn
    return deco


def enqueue(
    sess: Session,
    *,
    team_id: int,
    kind: str,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> models.Job:
    """Enqueue a job. Idempotent on ``(team_id, kind, idempotency_key)``."""
    if idempotency_key:
        existing = sess.execute(
            select(models.Job).where(
                models.Job.team_id == team_id,
                models.Job.kind == kind,
                models.Job.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
    job = models.Job(
        team_id=team_id,
        kind=kind,
        status="queued",
        payload=json.dumps(payload, sort_keys=True) if payload else None,
        idempotency_key=idempotency_key,
    )
    sess.add(job)
    sess.flush()
    return job


def _claim_one(sess: Session) -> Optional[models.Job]:
    """Atomically claim the oldest queued job. SQLite-safe (single-writer)."""
    job = sess.execute(
        select(models.Job)
        .where(models.Job.status == "queued")
        .order_by(models.Job.id)
        .limit(1)
    ).scalar_one_or_none()
    if job is None:
        return None
    job.status = "running"
    job.started_at = now_utc().replace(tzinfo=None)
    job.attempts += 1
    sess.flush()
    return job


def _run_one(sess: Session, job: models.Job) -> None:
    handler = _HANDLERS.get(job.kind)
    if handler is None:
        job.status = "failed"
        job.error = f"no handler registered for kind {job.kind!r}"
        job.finished_at = now_utc().replace(tzinfo=None)
        return
    payload = json.loads(job.payload) if job.payload else {}
    try:
        result = handler(sess, job, payload) or {}
        job.status = "completed"
        job.result = json.dumps(result, sort_keys=True, default=str)
    except Exception as exc:  # noqa: BLE001
        job.status = "failed"
        job.error = f"{exc}\n{traceback.format_exc()}"[:4000]
        log.exception("job failed: id=%s kind=%s", job.id, job.kind)
    finally:
        job.finished_at = now_utc().replace(tzinfo=None)


def run_once() -> bool:
    """Process at most one job. Returns True if a job was processed."""
    SessionLocal = get_session_factory()
    with SessionLocal() as sess:
        job = _claim_one(sess)
        if job is None:
            sess.commit()
            return False
        sess.commit()
    # Run in its own transaction so a failing job doesn't roll back the claim.
    with SessionLocal() as sess:
        job = sess.get(models.Job, job.id)
        if job is None:
            return False
        _run_one(sess, job)
        sess.commit()
        return True


def run_forever(*, idle_sleep_s: float = 1.0, stop_after_idle: float | None = None) -> None:
    """Block forever consuming jobs. ``stop_after_idle`` is for tests."""
    log.info("worker_start handlers=%s", sorted(_HANDLERS.keys()))
    idle_time = 0.0
    while True:
        did = run_once()
        if did:
            idle_time = 0.0
            continue
        time.sleep(idle_sleep_s)
        idle_time += idle_sleep_s
        if stop_after_idle is not None and idle_time >= stop_after_idle:
            return


# ---------------------------------------------------------------------------
# Built-in handlers
# ---------------------------------------------------------------------------
@register("extract_meeting")
def _handle_extract_meeting(sess: Session, job: models.Job, payload: dict[str, Any]):
    """Background extraction for an existing meeting."""
    from . import services
    from .audit import record as audit_record
    from .extraction import extract
    from .webhooks import emit as emit_webhook

    meeting_id = int(payload["meeting_id"])
    meeting = sess.get(models.Meeting, meeting_id)
    if meeting is None or meeting.team_id != job.team_id:
        raise ValueError(f"meeting {meeting_id} not found for team {job.team_id}")
    if meeting.finalized:
        raise ValueError("meeting is finalized")
    result = extract(meeting.transcript + "\n" + meeting.notes,
                     reference=meeting.occurred_at)
    services.persist_extraction(sess, meeting, result)
    audit_record(
        sess, team_id=job.team_id, action="meeting.extracted",
        entity_type="meeting", entity_id=meeting.id,
        metadata={"tasks": len(meeting.tasks), "decisions": len(meeting.decisions)},
    )
    emit_webhook(sess, team_id=job.team_id, event="meeting.extracted", payload={
        "meeting_id": meeting.id,
        "tasks": len(meeting.tasks),
        "decisions": len(meeting.decisions),
    })
    return {
        "meeting_id": meeting.id,
        "tasks_extracted": len(meeting.tasks),
        "decisions_extracted": len(meeting.decisions),
    }


@register("dispatch_webhooks")
def _handle_dispatch_webhooks(sess: Session, job: models.Job, payload: dict[str, Any]):
    """Drain the webhook delivery queue."""
    from .webhooks import deliver_pending
    delivered = deliver_pending(sess)
    return {"delivered": delivered}
