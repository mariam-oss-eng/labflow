"""Distributed worker leader-lock (v0.7).

Use case: when LabFlow is deployed as multiple replicas behind a load
balancer (the recommended HA topology), the in-process job worker
should run on **at most one** replica at a time so no job is double-
executed and audit/webhook side effects stay exactly-once. We achieve
this with a tiny DB-backed lease — works on both SQLite and Postgres
without any extension.

Acquire semantics
-----------------
* Each candidate writes ``(name, owner, expires_at)`` to ``worker_locks``.
* If a row already exists, the candidate updates only when
  ``expires_at < now()``. The update returns ``rowcount == 1`` exactly
  for the winner; losers see ``0`` and back off.
* The winner periodically refreshes ``expires_at`` (heartbeat) while
  it's still alive. If the process crashes, the lease naturally expires
  and the next replica takes over within ``ttl_seconds``.

This is not Paxos — it's the standard "DB-as-coordinator" pattern that
Sidekiq, Resque, and Celery's ``--without-heartbeat`` modes all reduce
to. Adequate for LabFlow's MVP. Swap to Redis ``SET NX PX`` or Postgres
advisory locks if you need sub-second handover.
"""
from __future__ import annotations

import logging
import os
import socket
import uuid
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models
from .time_utils import now_utc

log = logging.getLogger("labflow.worker_lock")

DEFAULT_TTL = 30  # seconds


def _new_owner() -> str:
    return f"{socket.gethostname()}/{os.getpid()}/{uuid.uuid4().hex[:8]}"


def acquire(
    sess: Session, *, name: str, owner: str | None = None,
    ttl_seconds: int = DEFAULT_TTL,
) -> str | None:
    """Try to acquire ``name``. Returns the owner string on success, else None."""
    owner = owner or _new_owner()
    now = now_utc().replace(tzinfo=None)
    expires = now + timedelta(seconds=ttl_seconds)
    existing = sess.get(models.WorkerLock, name)
    if existing is None:
        try:
            sess.add(models.WorkerLock(
                name=name, owner=owner, acquired_at=now, expires_at=expires,
            ))
            sess.flush()
            sess.commit()
            return owner
        except IntegrityError:
            sess.rollback()
            existing = sess.get(models.WorkerLock, name)
    if existing is None:
        return None
    if existing.expires_at > now and existing.owner != owner:
        return None
    # Re-acquire by us, or steal from an expired holder.
    res = sess.execute(
        update(models.WorkerLock)
        .where(
            models.WorkerLock.name == name,
            (models.WorkerLock.expires_at < now) | (models.WorkerLock.owner == owner),
        )
        .values(owner=owner, acquired_at=now, expires_at=expires)
    )
    sess.commit()
    if res.rowcount == 1:
        return owner
    return None


def heartbeat(
    sess: Session, *, name: str, owner: str, ttl_seconds: int = DEFAULT_TTL,
) -> bool:
    """Extend the lease. Returns False if we no longer hold it."""
    now = now_utc().replace(tzinfo=None)
    expires = now + timedelta(seconds=ttl_seconds)
    res = sess.execute(
        update(models.WorkerLock)
        .where(
            models.WorkerLock.name == name,
            models.WorkerLock.owner == owner,
        )
        .values(expires_at=expires)
    )
    sess.commit()
    return res.rowcount == 1


def release(sess: Session, *, name: str, owner: str) -> None:
    """Release the lock if we still own it. Always succeeds."""
    now = now_utc().replace(tzinfo=None)
    sess.execute(
        update(models.WorkerLock)
        .where(
            models.WorkerLock.name == name,
            models.WorkerLock.owner == owner,
        )
        # Mark expired in the past so a new owner can take over instantly.
        .values(expires_at=now - timedelta(seconds=1))
    )
    sess.commit()
