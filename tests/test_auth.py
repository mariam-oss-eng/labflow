"""Tests for API-key authentication and team isolation."""
from __future__ import annotations

import pytest

from labflow import config as config_mod
from labflow import db as db_mod
from labflow.auth import generate_api_key, hash_api_key
from labflow.models import ApiKey, Team


@pytest.fixture()
def auth_client(temp_db, monkeypatch):
    """App client running with auth enforced."""
    monkeypatch.setenv("LABFLOW_AUTH_ENABLED", "true")
    monkeypatch.setenv("LABFLOW_BOOTSTRAP_TEAM", "acme")
    monkeypatch.setenv("LABFLOW_BOOTSTRAP_API_KEY", "lfk_testkey_acme")
    config_mod.reset_settings_cache()

    from fastapi.testclient import TestClient
    from labflow.main import create_app

    app = create_app()
    return TestClient(app)


def _make_team(slug: str, key_plain: str):
    """Create a team + api key directly in the DB."""
    from labflow.time_utils import now_utc

    SessionLocal = db_mod.get_session_factory()
    with SessionLocal() as s:
        team = Team(slug=slug, name=slug.title(), created_at=now_utc())
        s.add(team)
        s.flush()
        s.add(ApiKey(team_id=team.id, name="test", key_hash=hash_api_key(key_plain),
                     created_at=now_utc()))
        s.commit()
        return team.id


def test_request_without_key_is_unauthorized(auth_client):
    r = auth_client.post("/api/meetings", json={"title": "x", "transcript": "We decided X."})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_request_with_invalid_key_is_unauthorized(auth_client):
    r = auth_client.post(
        "/api/meetings",
        json={"title": "x", "transcript": "We decided X."},
        headers={"Authorization": "Bearer lfk_bogus"},
    )
    assert r.status_code == 401


def test_request_with_valid_bootstrap_key_works(auth_client):
    r = auth_client.post(
        "/api/meetings",
        json={"title": "x", "transcript": "We decided X."},
        headers={"Authorization": "Bearer lfk_testkey_acme"},
    )
    assert r.status_code == 201


def test_x_labflow_key_header_works(auth_client):
    r = auth_client.post(
        "/api/meetings",
        json={"title": "x", "transcript": "We decided X."},
        headers={"X-LabFlow-Key": "lfk_testkey_acme"},
    )
    assert r.status_code == 201


def test_team_isolation(auth_client):
    # Create a second team with its own key.
    second_key = "lfk_testkey_other"
    _make_team("other", second_key)

    # Team "acme" creates a meeting.
    acme_headers = {"Authorization": "Bearer lfk_testkey_acme"}
    other_headers = {"Authorization": f"Bearer {second_key}"}

    m = auth_client.post(
        "/api/meetings",
        json={"title": "acme-private", "transcript": "We decided X."},
        headers=acme_headers,
    ).json()
    mid = m["id"]

    # Other team cannot read it.
    r = auth_client.get(f"/api/meetings/{mid}/export.json", headers=other_headers)
    assert r.status_code == 404

    # Other team's listing does not include it.
    body = auth_client.get("/api/meetings", headers=other_headers).json()
    assert all(item["id"] != mid for item in body["items"])

    # Acme team can still read it.
    r = auth_client.get(f"/api/meetings/{mid}/export.json", headers=acme_headers)
    assert r.status_code == 200


def test_revoked_key_rejected(auth_client):
    from labflow.time_utils import now_utc

    SessionLocal = db_mod.get_session_factory()
    with SessionLocal() as s:
        keys = s.query(ApiKey).all()
        for k in keys:
            k.revoked_at = now_utc()
        s.commit()

    r = auth_client.post(
        "/api/meetings",
        json={"title": "x", "transcript": "y"},
        headers={"Authorization": "Bearer lfk_testkey_acme"},
    )
    assert r.status_code == 401


def test_generate_api_key_format():
    k = generate_api_key()
    assert k.startswith("lfk_")
    assert len(k) > 30
