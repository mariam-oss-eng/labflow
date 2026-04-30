"""Read-replica routing (v0.9).

Production deployments often have a primary write database and one or
more read replicas. This module exposes :func:`read_session` and
:func:`write_session` context managers; reads are round-robin across
``LABFLOW_READ_REPLICA_URLS`` (when configured), writes go to the
primary.

The router is **opt-in per call site** — existing code that uses
:func:`labflow.db.session_scope` continues to hit the primary. Endpoints
that are read-only (``GET`` analytics, search, share-link resolution)
can switch to ``with read_session() as s:`` to offload load.

Replica lag handling
--------------------
The router does NOT try to enforce read-after-write consistency. Callers
that just wrote and need to read their own write should use
``write_session()`` for both. A future enhancement could thread a
"prefer-primary" hint through ``Request.state``.
"""
from __future__ import annotations

import itertools
import logging
import threading
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .db import get_engine, get_session_factory

log = logging.getLogger("labflow.replica")

_replica_engines: list = []
_replica_sessions: list = []
_replica_iter: itertools.cycle | None = None
_lock = threading.Lock()


def _ensure_replicas() -> None:
    """Lazy-build engine + sessionmaker for each replica URL."""
    global _replica_iter
    settings = get_settings()
    urls = list(settings.read_replica_urls or [])
    with _lock:
        if _replica_engines and len(_replica_engines) == len(urls):
            return
        _replica_engines.clear()
        _replica_sessions.clear()
        for url in urls:
            connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
            eng = create_engine(url, connect_args=connect_args, future=True)
            _replica_engines.append(eng)
            _replica_sessions.append(
                sessionmaker(bind=eng, expire_on_commit=False, future=True)
            )
        _replica_iter = itertools.cycle(_replica_sessions) if _replica_sessions else None


def reset_replicas_for_tests() -> None:
    global _replica_iter
    with _lock:
        for eng in _replica_engines:
            try:
                eng.dispose()
            except Exception:  # noqa: BLE001
                pass
        _replica_engines.clear()
        _replica_sessions.clear()
        _replica_iter = None


@contextmanager
def write_session() -> Iterator[Session]:
    """Yield a session bound to the primary, committing on success."""
    SF = get_session_factory()
    s = SF()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


@contextmanager
def read_session() -> Iterator[Session]:
    """Yield a *read-only* session.

    Falls back to the primary when no replicas are configured. The
    yielded session never commits; ``rollback()`` happens on exit so any
    accidental writes are discarded.
    """
    _ensure_replicas()
    if _replica_iter is None:
        SF = get_session_factory()
    else:
        SF = next(_replica_iter)
    s = SF()
    try:
        yield s
    finally:
        try:
            s.rollback()
        except Exception:  # noqa: BLE001
            pass
        s.close()


def health() -> dict:
    """Return a dict suitable for ``/readyz`` describing replica status."""
    _ensure_replicas()
    out: dict = {
        "primary": str(get_engine().url).split("@")[-1],
        "replicas": [],
    }
    for eng in _replica_engines:
        try:
            with eng.connect() as conn:
                from sqlalchemy import text
                conn.execute(text("SELECT 1"))
            out["replicas"].append({"url": str(eng.url).split("@")[-1], "ok": True})
        except Exception as e:  # noqa: BLE001
            out["replicas"].append({"url": str(eng.url).split("@")[-1],
                                    "ok": False, "error": str(e)[:120]})
    return out
