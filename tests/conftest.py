"""Shared pytest fixtures.

Each test gets its own SQLite database so we never share state between tests.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def temp_db(monkeypatch):
    """Create an isolated SQLite database for the test, then tear it down."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="labflow-test-"))
    db_path = tmp_dir / "test.db"
    url = f"sqlite:///{db_path}"
    monkeypatch.setenv("LABFLOW_DATABASE_URL", url)

    # Reset cached engine/session in the db module so they pick up the new URL.
    from labflow import db as db_mod

    db_mod._engine = None
    db_mod._SessionLocal = None
    db_mod.init_db()
    try:
        yield db_mod
    finally:
        db_mod._engine = None
        db_mod._SessionLocal = None
        if db_path.exists():
            db_path.unlink()
        try:
            tmp_dir.rmdir()
        except OSError:
            pass


@pytest.fixture()
def session(temp_db):
    SessionLocal = temp_db.get_session_factory()
    sess = SessionLocal()
    try:
        yield sess
        sess.rollback()
    finally:
        sess.close()


@pytest.fixture()
def app_client(temp_db):
    """Build a fresh FastAPI app + TestClient bound to the temp DB."""
    from fastapi.testclient import TestClient

    from labflow.main import create_app

    app = create_app()
    return TestClient(app)
