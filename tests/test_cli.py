"""Tests for the labflow CLI."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_cli_team_create_and_keys_create(temp_db, capsys):
    from labflow.cli import main

    rc = main(["team", "create", "acme", "--name", "Acme"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "created team" in out

    rc = main(["keys", "create", "acme", "--name", "ci"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "lfk_" in out


def test_cli_team_create_duplicate_returns_nonzero(temp_db, capsys):
    from labflow.cli import main
    main(["team", "create", "acme"])
    capsys.readouterr()
    rc = main(["team", "create", "acme"])
    assert rc == 1


def test_cli_keys_revoke(temp_db, capsys):
    from labflow import db as db_mod
    from labflow import models
    from labflow.cli import main

    main(["team", "create", "acme"])
    main(["keys", "create", "acme"])
    capsys.readouterr()

    SessionLocal = db_mod.get_session_factory()
    with SessionLocal() as s:
        kid = s.query(models.ApiKey).first().id

    rc = main(["keys", "revoke", str(kid)])
    assert rc == 0
    with SessionLocal() as s:
        key = s.get(models.ApiKey, kid)
        assert key.revoked_at is not None


def test_cli_ingest(temp_db, tmp_path: Path, capsys):
    from labflow.cli import main
    f = tmp_path / "t.txt"
    f.write_text("@alice will retrain the model tomorrow.")
    rc = main(["ingest", str(f), "--team", "default", "--title", "test"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "tasks=" in out


def test_cli_digest(temp_db, capsys):
    from labflow.cli import main
    rc = main(["digest", "--team", "default"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Weekly Digest" in out
