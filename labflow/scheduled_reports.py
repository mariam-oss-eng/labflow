"""Scheduled reports — saved LFQL queries that fire on a cadence (v0.16).

A report row is::

    {
      "name": "open-bugs-daily",
      "query": "status:open AND title:bug",
      "cadence": "daily",
      "webhook_url": "https://hooks.example/lf",
      "secret": "..."   # optional, signs HMAC-SHA256 over the body
    }

The sweeper (:func:`run_due`) finds reports whose ``next_run_at`` is in
the past, executes the LFQL query, POSTs the matching task IDs to
``webhook_url`` (HMAC-signed if a secret is set), records a
:class:`labflow.models.ScheduledReportRun`, and advances ``next_run_at``.

We deliberately don't reuse the generic ``WebhookSubscription`` queue
because reports are pull-driven (the schedule decides) rather than
event-driven, and we want their delivery history to be queryable
on its own without competing with regular webhook traffic.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import urllib.error
import urllib.request
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit as audit_mod, lfql, models
from .errors import ConflictError, NotFoundError, ValidationError
from .time_utils import now_utc

log = logging.getLogger(__name__)

VALID_CADENCES = {
    "hourly": timedelta(hours=1),
    "daily":  timedelta(days=1),
    "weekly": timedelta(days=7),
}

_HTTP_TIMEOUT_S = 10.0
_BODY_PREVIEW = 512


def _next_after(now, cadence: str):
    return now + VALID_CADENCES[cadence]


def create(
    sess: Session, *, team_id: int, name: str, query: str,
    cadence: str, webhook_url: str, secret: str | None = None,
    actor: str = "system",
) -> models.ScheduledReport:
    if not name or not isinstance(name, str) or len(name) > 120:
        raise ValidationError("name must be 1–120 chars")
    if cadence not in VALID_CADENCES:
        raise ValidationError(
            f"cadence must be one of {sorted(VALID_CADENCES)}"
        )
    if not webhook_url.startswith(("http://", "https://")):
        raise ValidationError("webhook_url must be http(s)")
    # Validate the query by parsing it.
    lfql.parse(query)

    existing = sess.execute(
        select(models.ScheduledReport).where(
            models.ScheduledReport.team_id == team_id,
            models.ScheduledReport.name == name,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError(f"report named {name!r} already exists")

    now = now_utc()
    row = models.ScheduledReport(
        team_id=team_id, name=name, query=query, cadence=cadence,
        webhook_url=webhook_url, secret=secret,
        next_run_at=_next_after(now, cadence), enabled=True,
    )
    sess.add(row)
    sess.flush()
    audit_mod.record(
        sess, team_id=team_id, action="report.create",
        entity_type="scheduled_report", entity_id=row.id, actor=actor,
        metadata={"name": name, "cadence": cadence},
    )
    return row


def list_reports(
    sess: Session, *, team_id: int,
) -> list[models.ScheduledReport]:
    return list(sess.execute(
        select(models.ScheduledReport)
        .where(models.ScheduledReport.team_id == team_id)
        .order_by(models.ScheduledReport.id)
    ).scalars())


def disable(
    sess: Session, *, team_id: int, report_id: int, actor: str = "system",
) -> models.ScheduledReport:
    row = sess.get(models.ScheduledReport, report_id)
    if row is None or row.team_id != team_id:
        raise NotFoundError(f"report {report_id} not found")
    row.enabled = False
    sess.flush()
    audit_mod.record(
        sess, team_id=team_id, action="report.disable",
        entity_type="scheduled_report", entity_id=row.id, actor=actor,
    )
    return row


def delete(
    sess: Session, *, team_id: int, report_id: int, actor: str = "system",
) -> None:
    row = sess.get(models.ScheduledReport, report_id)
    if row is None or row.team_id != team_id:
        raise NotFoundError(f"report {report_id} not found")
    sess.delete(row)
    sess.flush()
    audit_mod.record(
        sess, team_id=team_id, action="report.delete",
        entity_type="scheduled_report", entity_id=report_id, actor=actor,
    )


def runs_for(
    sess: Session, *, team_id: int, report_id: int, limit: int = 50,
) -> list[models.ScheduledReportRun]:
    row = sess.get(models.ScheduledReport, report_id)
    if row is None or row.team_id != team_id:
        raise NotFoundError(f"report {report_id} not found")
    return list(sess.execute(
        select(models.ScheduledReportRun)
        .where(models.ScheduledReportRun.report_id == report_id)
        .order_by(models.ScheduledReportRun.id.desc())
        .limit(limit)
    ).scalars())


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256,
    ).hexdigest()


def _default_post(url: str, body: bytes,
                  headers: dict[str, str]) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310
            return resp.status, resp.read(_BODY_PREVIEW).decode(
                "utf-8", errors="replace"
            )
    except urllib.error.HTTPError as e:
        body_preview = ""
        if e.fp is not None:
            body_preview = e.read(_BODY_PREVIEW).decode(
                "utf-8", errors="replace"
            )
        return e.code, body_preview


def execute_one(
    sess: Session, *, report: models.ScheduledReport,
    http_post=None,
) -> models.ScheduledReportRun:
    """Run one report exactly once. Records a run row regardless of outcome."""
    poster = http_post or _default_post
    matched_ids: list[int] = []
    error: str | None = None
    status_code: int | None = None
    delivered = False

    try:
        tasks = lfql.run(sess, team_id=report.team_id,
                         query=report.query, limit=1000)
        matched_ids = [t.id for t in tasks]
        body = json.dumps({
            "report": report.name,
            "matched_task_ids": matched_ids,
        }, sort_keys=True).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-LabFlow-Report": report.name,
        }
        if report.secret:
            headers["X-LabFlow-Signature-256"] = _sign(body, report.secret)
        try:
            status_code, _resp = poster(report.webhook_url, body, headers)
            delivered = 200 <= status_code < 300
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"[:500]
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"[:500]

    run = models.ScheduledReportRun(
        report_id=report.id, matched=len(matched_ids),
        delivered=delivered, status_code=status_code, error=error,
    )
    sess.add(run)

    now = now_utc()
    report.last_run_at = now
    report.next_run_at = _next_after(now, report.cadence)
    sess.flush()
    return run


def run_due(sess: Session, *, http_post=None, limit: int = 100) -> int:
    """Find every enabled report whose ``next_run_at`` is in the past
    and execute it. Returns the number of reports executed."""
    now = now_utc()
    due = list(sess.execute(
        select(models.ScheduledReport)
        .where(
            models.ScheduledReport.enabled.is_(True),
            models.ScheduledReport.next_run_at.isnot(None),
            models.ScheduledReport.next_run_at <= now,
        )
        .limit(limit)
    ).scalars())
    for r in due:
        execute_one(sess, report=r, http_post=http_post)
    return len(due)


__all__ = [
    "VALID_CADENCES", "create", "list_reports", "disable", "delete",
    "runs_for", "execute_one", "run_due",
]
