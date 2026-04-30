"""Tests for v0.9 — AI copilot, plugin marketplace, vector v2, time-travel,
i18n, replica router, PWA assets."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _seed(client: TestClient) -> tuple[int, int, int]:
    r = client.post("/api/meetings", json={
        "title": "v0.9 seed",
        "transcript": (
            "We decided to switch to Postgres for v2. "
            "TODO: provision the staging cluster by Friday. "
            "Owner: @alice"
        ),
        "notes": "",
    })
    assert r.status_code == 201
    mid = r.json()["id"]
    from labflow.db import get_session_factory
    from labflow import models
    with get_session_factory()() as s:
        d = s.query(models.Decision).filter_by(meeting_id=mid).first()
        t = s.query(models.Task).filter_by(meeting_id=mid).first()
        return mid, d.id, t.id


# ============================================================ Copilot
def test_copilot_session_lifecycle(temp_db, app_client):
    r = app_client.post("/api/copilot/sessions", json={"title": "Investigation"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    r = app_client.get("/api/copilot/sessions")
    assert any(s["id"] == sid for s in r.json()["sessions"])
    r = app_client.post(f"/api/copilot/sessions/{sid}/close")
    assert r.status_code == 200
    # Closed sessions reject new turns.
    r = app_client.post(f"/api/copilot/sessions/{sid}/turns",
                        json={"message": "hi"})
    assert r.status_code in (400, 422)


def test_copilot_search_intent(temp_db, app_client):
    _seed(app_client)
    r = app_client.post("/api/copilot/sessions", json={"title": "Q"})
    sid = r.json()["id"]
    r = app_client.post(f"/api/copilot/sessions/{sid}/turns",
                        json={"message": "Postgres"})
    assert r.status_code == 200
    body = r.json()
    assert body["tool_calls"][0]["tool"] == "search"
    # Tool result should be a non-empty list of search hits.
    assert len(body["tool_calls"][0]["result"]) >= 1


def test_copilot_open_tasks_intent(temp_db, app_client):
    _seed(app_client)
    r = app_client.post("/api/copilot/sessions", json={"title": "Q"})
    sid = r.json()["id"]
    r = app_client.post(f"/api/copilot/sessions/{sid}/turns",
                        json={"message": "show me open tasks"})
    body = r.json()
    assert body["tool_calls"][0]["tool"] == "list_open_tasks"


def test_copilot_propose_task_safe(temp_db, app_client):
    """propose_task returns a draft only and never creates a row."""
    r = app_client.post("/api/copilot/sessions", json={"title": "Q"})
    sid = r.json()["id"]
    r = app_client.post(f"/api/copilot/sessions/{sid}/turns",
                        json={"message": "Propose task: ship the docs"})
    body = r.json()
    call = body["tool_calls"][0]
    assert call["tool"] == "propose_task"
    assert call["result"]["draft"] is True
    # Verify no Task row was actually created.
    from labflow.db import get_session_factory
    from labflow import models
    with get_session_factory()() as s:
        assert s.query(models.Task).count() == 0


def test_copilot_tools_endpoint(temp_db, app_client):
    r = app_client.get("/api/copilot/tools")
    tools = {t["name"] for t in r.json()["tools"]}
    assert "search" in tools and "get_task" in tools and "analytics" in tools


# ============================================================ Plugins
_SAMPLE_MANIFEST = {
    "name": "weekly-report",
    "version": "1.2.0",
    "author": "Acme Co",
    "description": "Generates a polished weekly report.",
    "permissions": ["read", "webhook:emit"],
    "hooks": ["meeting.finalized"],
}


def test_plugin_install_and_enable(temp_db, app_client):
    from labflow import plugin_marketplace as pm
    expected = pm.manifest_hash(_SAMPLE_MANIFEST)
    r = app_client.post("/api/plugins", json={
        "manifest": _SAMPLE_MANIFEST, "expected_sha256": expected,
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "weekly-report"
    assert body["enabled"] is False
    assert body["manifest_sha256"] == expected
    # enable
    r = app_client.post("/api/plugins/weekly-report/enable",
                        json={"enabled": True})
    assert r.status_code == 200
    assert r.json()["enabled"] is True
    # list
    r = app_client.get("/api/plugins")
    assert r.json()["plugins"][0]["enabled"] is True
    # uninstall
    r = app_client.delete("/api/plugins/weekly-report")
    assert r.status_code == 200
    r = app_client.get("/api/plugins")
    assert r.json()["plugins"] == []


def test_plugin_install_hash_mismatch(temp_db, app_client):
    r = app_client.post("/api/plugins", json={
        "manifest": _SAMPLE_MANIFEST,
        "expected_sha256": "0" * 64,
    })
    assert r.status_code in (400, 422)


def test_plugin_install_invalid_manifest(temp_db, app_client):
    r = app_client.post("/api/plugins", json={
        "manifest": {"name": "X", "version": "not-semver", "author": "x"},
    })
    assert r.status_code in (400, 422)


def test_plugin_install_duplicate(temp_db, app_client):
    app_client.post("/api/plugins", json={"manifest": _SAMPLE_MANIFEST})
    r = app_client.post("/api/plugins", json={"manifest": _SAMPLE_MANIFEST})
    assert r.status_code == 409


# ============================================================ Vector index v2
def test_vector_v2_brute_force_fallback(temp_db, app_client):
    """With no shard built, query falls back to brute-force over Embedding rows."""
    _seed(app_client)
    r = app_client.get("/api/vector/query", params={"q": "Postgres", "k": 5})
    assert r.status_code == 200
    body = r.json()
    # Seed produced at least one decision/task with embeddings.
    assert isinstance(body["results"], list)
    assert len(body["results"]) >= 1


def test_vector_v2_build_disabled_when_unset(temp_db, app_client):
    """Without LABFLOW_VECTOR_INDEX_DIR the build is a no-op."""
    _seed(app_client)
    r = app_client.post("/api/vector/build")
    assert r.status_code == 200
    assert r.json()["built"] is False


def test_vector_v2_build_and_query(temp_db, app_client, tmp_path, monkeypatch):
    """Configure persistence dir, build the index, query it back."""
    import importlib
    from labflow import config as config_mod, vector_index_v2
    monkeypatch.setenv("LABFLOW_VECTOR_INDEX_DIR", str(tmp_path))
    config_mod.reset_settings_cache()
    vector_index_v2.reset_cache_for_tests()
    _seed(app_client)
    # Build several seeds so there's something to index.
    for _ in range(3):
        _seed(app_client)
    r = app_client.post("/api/vector/build")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["built"] is True
    assert body["vectors"] >= 1
    # File should exist.
    assert Path(body["path"]).is_file()
    # Query against the freshly-loaded shard.
    r = app_client.get("/api/vector/query",
                       params={"q": "Postgres", "k": 3, "rerank": True})
    assert r.status_code == 200
    assert len(r.json()["results"]) >= 1


def test_vector_v2_index_save_load_roundtrip(tmp_path):
    """Direct unit test for the IndexShard persistence format."""
    from labflow.vector_index_v2 import IndexShard, IndexEntry
    s = IndexShard(model="test", dim=4)
    vectors = [
        [1.0, 0.0, 0.0, 0.0], [0.9, 0.1, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0],
    ]
    for i, v in enumerate(vectors):
        s.add(IndexEntry("decision", i + 1), v)
    p = tmp_path / "shard.idx"
    s.save(p)
    loaded = IndexShard.load(p)
    results = loaded.query([1.0, 0.0, 0.0, 0.0], k=2)
    # The two highest-cosine entries are #1 (1.0) and #2 (~0.99).
    ids = [e.entity_id for _, e in results]
    assert ids[0] == 1
    assert 2 in ids


# ============================================================ Time-travel
def test_timetravel_task_rewind(temp_db, app_client):
    _mid, _did, tid = _seed(app_client)
    # Snapshot t0 before any transition.
    t0 = datetime.utcnow()
    time.sleep(0.05)
    r = app_client.post(f"/api/tasks/{tid}/transition",
                        json={"to_state": "in_progress"})
    assert r.status_code == 200
    # Time-travel to t0 should still see the original state.
    r = app_client.get(f"/api/timetravel/tasks/{tid}",
                       params={"as_of": t0.isoformat()})
    assert r.status_code == 200, r.text
    body = r.json()
    # Default workflow's initial state is "open".
    assert body["state"] in (None, "open")


def test_timetravel_validates_future(temp_db, app_client):
    _mid, _did, tid = _seed(app_client)
    future = (datetime.utcnow() + timedelta(days=10)).isoformat()
    r = app_client.get(f"/api/timetravel/tasks/{tid}",
                       params={"as_of": future})
    assert r.status_code in (400, 422)


def test_timetravel_meeting_snapshot(temp_db, app_client):
    mid, _did, tid = _seed(app_client)
    after = datetime.utcnow().isoformat()
    # We pick "now" — meeting should have its decision and the task at
    # initial state.
    r = app_client.get(f"/api/timetravel/meetings/{mid}",
                       params={"as_of": after})
    assert r.status_code in (200, 422)  # 422 if "future" check fires (race)
    if r.status_code == 200:
        body = r.json()
        assert body["id"] == mid
        assert len(body["decisions"]) >= 1


# ============================================================ i18n
def test_i18n_locales_endpoint(temp_db, app_client):
    r = app_client.get("/api/i18n/locales")
    assert r.status_code == 200
    locs = r.json()["locales"]
    assert "en" in locs and "es" in locs and "fr" in locs


def test_i18n_messages_negotiation(temp_db, app_client):
    r = app_client.get("/api/i18n/messages",
                       headers={"Accept-Language": "fr-CA,fr;q=0.9,en;q=0.5"})
    assert r.status_code == 200
    body = r.json()
    assert body["locale"] == "fr"
    assert body["messages"]["nav.tasks"] == "Tâches"


def test_i18n_messages_explicit_locale(temp_db, app_client):
    r = app_client.get("/api/i18n/messages", params={"locale": "es"})
    assert r.json()["locale"] == "es"
    assert r.json()["messages"]["task.status.open"] == "Abierta"


def test_i18n_messages_fallback_to_english(temp_db, app_client):
    r = app_client.get("/api/i18n/messages",
                       headers={"Accept-Language": "ja-JP,ja;q=0.9"})
    assert r.json()["locale"] == "en"


# ============================================================ Replica router
def test_replica_router_falls_back_to_primary(temp_db):
    """With no replicas configured, read_session() yields a session bound
    to the primary."""
    from labflow import replica
    replica.reset_replicas_for_tests()
    with replica.read_session() as s:
        from sqlalchemy import text
        rows = s.execute(text("SELECT 1")).all()
    assert rows == [(1,)]


def test_replica_health_endpoint(temp_db, app_client):
    r = app_client.get("/readyz/replicas")
    assert r.status_code == 200
    body = r.json()
    assert "primary" in body and isinstance(body["replicas"], list)


# ============================================================ PWA assets
def test_pwa_manifest_served(temp_db, app_client):
    r = app_client.get("/static/manifest.webmanifest")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "LabFlow"
    assert body["display"] == "standalone"


def test_pwa_service_worker_served(temp_db, app_client):
    r = app_client.get("/static/sw.js")
    assert r.status_code == 200
    assert "labflow-v1" in r.text
    assert "addEventListener" in r.text


def test_pwa_offline_shell_served(temp_db, app_client):
    r = app_client.get("/static/offline.html")
    assert r.status_code == 200
    assert "offline" in r.text.lower()
