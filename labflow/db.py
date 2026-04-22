"""Database engine and session helpers."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    """Declarative base class for all ORM models."""


_engine = None
_SessionLocal: sessionmaker | None = None


def get_engine():
    global _engine
    if _engine is None:
        url = get_settings().database_url
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args, future=True)
    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionLocal


def init_db() -> None:
    """Create all tables. Safe to call repeatedly."""
    # Import models so they are registered on Base.metadata.
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session context manager."""
    sess = get_session_factory()()
    try:
        yield sess
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()


def reset_engine_for_tests(url: str) -> None:
    """Reset module-level engine/session — used by the test suite."""
    global _engine, _SessionLocal
    _engine = None
    _SessionLocal = None
    os_env_set = url
    import os as _os

    _os.environ["LABFLOW_DATABASE_URL"] = os_env_set
