"""Data retention + GDPR-style export and erase (v0.5).

Two supported operations:

  * **Retention sweep** — drops rows older than a configured horizon
    from append-only tables that grow forever (``audit_events``,
    ``webhook_deliveries``, ``idempotency_records``). Wired up as a
    background job (``retention_sweep`` kind) the worker runs hourly.
  * **Team export / erase** — produces a single JSON document of every
    row associated with a team (export), or unconditionally deletes
    them (erase). These are the data-subject-rights endpoints required
    by GDPR Article 15 (right of access) and Article 17 (right to
    erasure).

We do NOT soft-delete — erasure is a hard ``DELETE``. That's the only
way to honour an erasure request truthfully. Backups are a separate
concern documented in ``docs/operations.md``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import models
from .time_utils import now_utc


# ---------------------------------------------------------------------------
# Retention sweep
# ---------------------------------------------------------------------------
@dataclass
class RetentionPolicy:
    audit_event_days: int = 365
    webhook_delivery_days: int = 30
    idempotency_record_days: int = 1
    completed_job_days: int = 30


def sweep(sess: Session, policy: RetentionPolicy | None = None) -> dict[str, int]:
    """Apply ``policy``. Returns a count of rows deleted per table."""
    pol = policy or RetentionPolicy()
    now = now_utc().replace(tzinfo=None)
    deleted: dict[str, int] = {}

    cutoff = now - timedelta(days=pol.audit_event_days)
    deleted["audit_events"] = sess.execute(
        delete(models.AuditEvent).where(models.AuditEvent.created_at < cutoff)
    ).rowcount or 0

    cutoff = now - timedelta(days=pol.webhook_delivery_days)
    deleted["webhook_deliveries"] = sess.execute(
        delete(models.WebhookDelivery).where(models.WebhookDelivery.created_at < cutoff)
    ).rowcount or 0

    cutoff = now - timedelta(days=pol.idempotency_record_days)
    deleted["idempotency_records"] = sess.execute(
        delete(models.IdempotencyRecord).where(
            models.IdempotencyRecord.expires_at < now
        )
    ).rowcount or 0

    cutoff = now - timedelta(days=pol.completed_job_days)
    deleted["jobs_completed"] = sess.execute(
        delete(models.Job).where(
            models.Job.status.in_(("completed", "failed")),
            models.Job.finished_at < cutoff,
        )
    ).rowcount or 0

    sess.flush()
    return deleted


# ---------------------------------------------------------------------------
# Team export
# ---------------------------------------------------------------------------
def export_team(sess: Session, *, team_id: int) -> dict[str, Any]:
    """Return a JSON-serializable snapshot of every row for ``team_id``."""
    team = sess.get(models.Team, team_id)
    if team is None:
        return {"team": None}

    def _rows(model):
        return [_to_dict(r) for r in sess.execute(
            select(model).where(model.team_id == team_id)
        ).scalars()]

    return {
        "exported_at": now_utc().isoformat(),
        "schema_version": "0.5",
        "team": _to_dict(team),
        "owners": _rows(models.Owner),
        "meetings": _rows(models.Meeting),
        "decisions": _rows(models.Decision),
        "tasks": _rows(models.Task),
        "experiments": _rows(models.Experiment),
        "assumptions": _rows(models.Assumption),
        "blockers": _rows(models.Blocker),
        "evidence": _rows(models.Evidence),
        "audit_events": _rows(models.AuditEvent),
        "jobs": _rows(models.Job),
        "embeddings": _rows(models.Embedding),
    }


def erase_team(sess: Session, *, team_id: int) -> int:
    """Delete a team and every cascaded row. Returns rows touched (best-effort)."""
    team = sess.get(models.Team, team_id)
    if team is None:
        return 0
    sess.delete(team)
    sess.flush()
    return 1


def _to_dict(row: Any) -> dict:
    """Best-effort row → dict using SQLAlchemy column metadata."""
    if row is None:
        return {}
    out: dict[str, Any] = {}
    for col in row.__table__.columns:
        v = getattr(row, col.name, None)
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        # Decode JSON-ish text columns that we know about.
        if col.name == "vector" and isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:  # noqa: BLE001
                pass
        out[col.name] = v
    return out
