"""Tests for hybrid search, decision graph, rate limit, idempotency,
RBAC, encryption, SSE, plugins, and retention (v0.4 + v0.5)."""
from __future__ import annotations

import json
import os

import pytest

from labflow import models, services
from labflow.extraction import extract


# ---------------------------------------------------------------------------
# Hybrid search
# ---------------------------------------------------------------------------
def _seed_meetings(session, team):
    for txt, title in [
        ("We decided to adopt SentencePiece for tokenization", "m1"),
        ("@alice will retrain the model on the new dataset by Friday", "m2"),
        ("Hire two SREs in Q3 to cover the on-call rotation", "m3"),
    ]:
        m = models.Meeting(team_id=team.id, title=title, transcript=txt)
        session.add(m)
        session.flush()
        services.persist_extraction(session, m, extract(txt))
    session.flush()


def test_search_finds_lexical_match(session, default_team):
    from labflow.search import search
    _seed_meetings(session, default_team)
    hits = search(session, team_id=default_team.id, query="sentencepiece")
    assert hits, "expected at least one hit for an exact lexical term"
    assert hits[0].score > 0
    assert hits[0].score_components is not None


def test_search_semantic_via_shared_term(session, default_team):
    from labflow.search import search
    _seed_meetings(session, default_team)
    # alpha=1.0 → pure semantic ranking. Query shares "sentencepiece" but
    # not "tokenization", so lexical alone would also match — what we care
    # about is that semantic ranking surfaces the right hit at the top.
    hits = search(session, team_id=default_team.id, query="sentencepiece", alpha=1.0)
    assert hits
    titles = " ".join(h.title.lower() for h in hits[:1])
    assert "sentencepiece" in titles


def test_search_alpha_zero_disables_semantic(session, default_team):
    from labflow.search import search
    _seed_meetings(session, default_team)
    # alpha=0 → no purely-semantic neighbors should bubble up. A query
    # with no shared lexical tokens returns nothing.
    hits = search(session, team_id=default_team.id, query="kubernetes", alpha=0.0)
    assert hits == []


# ---------------------------------------------------------------------------
# Decision graph
# ---------------------------------------------------------------------------
def test_decision_graph_supersession(session, default_team):
    from labflow import graph as graph_mod
    # First meeting: original decision
    m1 = models.Meeting(team_id=default_team.id, title="m1",
                        transcript="We decided to use SentencePiece.")
    session.add(m1)
    session.flush()
    services.persist_extraction(session, m1, extract(m1.transcript))
    # Second meeting: same statement — should supersede.
    m2 = models.Meeting(team_id=default_team.id, title="m2",
                        transcript="We decided to use SentencePiece.")
    session.add(m2)
    session.flush()
    services.persist_extraction(session, m2, extract(m2.transcript))
    session.flush()

    g = graph_mod.build_graph(session, team_id=default_team.id)
    assert len(g.nodes) >= 2
    assert g.edges, "expected at least one supersession edge"

    mermaid = graph_mod.render_mermaid(g)
    assert mermaid.startswith("flowchart LR")
    assert "-->" in mermaid


def test_decision_graph_endpoint(app_client, monkeypatch):
    monkeypatch.setenv("LABFLOW_AUTH_ENABLED", "false")
    app_client.post("/api/meetings", json={
        "title": "m1",
        "transcript": "We decided to ship the v2 API.",
    })
    r = app_client.get("/api/graph/decisions")
    assert r.status_code == 200
    data = r.json()
    assert "nodes" in data and "edges" in data

    r = app_client.get("/api/graph/decisions.mermaid")
    assert r.status_code == 200
    assert r.text.startswith("flowchart LR")


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
def test_rate_limit_returns_429_with_retry_after(temp_db, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("LABFLOW_RATE_LIMIT_PER_MINUTE", "60")
    monkeypatch.setenv("LABFLOW_RATE_LIMIT_BURST", "3")
    from labflow import config as config_mod
    config_mod.reset_settings_cache()
    from labflow.main import create_app
    client = TestClient(create_app())

    # Exhaust the burst.
    for _ in range(3):
        r = client.get("/api/me")
        assert r.status_code == 200
        assert "x-ratelimit-limit" in r.headers
    # Next request should be 429.
    r = client.get("/api/me")
    assert r.status_code == 429
    assert "retry-after" in r.headers
    body = r.json()
    assert body["error"]["code"] == "rate_limited"


def test_rate_limit_exempts_health_and_metrics(temp_db, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("LABFLOW_RATE_LIMIT_PER_MINUTE", "60")
    monkeypatch.setenv("LABFLOW_RATE_LIMIT_BURST", "1")
    from labflow import config as config_mod
    config_mod.reset_settings_cache()
    from labflow.main import create_app
    client = TestClient(create_app())
    for _ in range(5):
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200
        assert client.get("/metrics").status_code == 200


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def test_idempotency_replays_response(app_client):
    payload = {"title": "kickoff",
               "transcript": "@alice will write the design doc."}
    headers = {"Idempotency-Key": "abc-123"}
    r1 = app_client.post("/api/meetings", json=payload, headers=headers)
    assert r1.status_code == 201
    body1 = r1.json()
    r2 = app_client.post("/api/meetings", json=payload, headers=headers)
    assert r2.status_code == 201
    assert r2.headers.get("idempotent-replay") == "true"
    assert r2.json()["id"] == body1["id"]

    # A different payload reusing the same key must 409.
    r3 = app_client.post("/api/meetings",
                         json={"title": "different", "transcript": "x"},
                         headers=headers)
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "idempotency_mismatch"


def test_idempotency_passes_through_without_header(app_client):
    payload = {"title": "no-key", "transcript": "x"}
    r1 = app_client.post("/api/meetings", json=payload)
    r2 = app_client.post("/api/meetings", json=payload)
    assert r1.status_code == 201 and r2.status_code == 201
    # Without the header these are two separate rows.
    assert r1.json()["id"] != r2.json()["id"]


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------
def test_rbac_viewer_blocked_from_admin(temp_db, monkeypatch):
    from fastapi.testclient import TestClient
    from labflow import auth as auth_mod
    from labflow.db import get_session_factory

    monkeypatch.setenv("LABFLOW_AUTH_ENABLED", "true")
    from labflow import config as config_mod
    config_mod.reset_settings_cache()
    from labflow.main import create_app
    app = create_app()
    SessionLocal = get_session_factory()
    with SessionLocal() as s:
        team = auth_mod.ensure_bootstrap_team(s)
        plaintext = auth_mod.generate_api_key()
        key = models.ApiKey(team_id=team.id, name="viewer",
                            key_hash=auth_mod.hash_api_key(plaintext))
        s.add(key)
        s.flush()
        s.add(models.Membership(team_id=team.id, api_key_id=key.id, role="viewer"))
        s.commit()

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {plaintext}"}
    # Read endpoint works.
    assert client.get("/api/me", headers=headers).status_code == 200
    # Admin endpoint is blocked.
    r = client.delete("/api/admin/erase", headers=headers)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Encryption at rest
# ---------------------------------------------------------------------------
def test_encrypted_text_roundtrips(temp_db, monkeypatch):
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("LABFLOW_DATA_KEY", key)
    from labflow import config as config_mod
    config_mod.reset_settings_cache()

    from labflow.db import get_session_factory
    from labflow.auth import ensure_bootstrap_team
    SessionLocal = get_session_factory()
    secret = "internal model weights are stored in s3://bucket/secret"
    with SessionLocal() as s:
        team = ensure_bootstrap_team(s)
        m = models.Meeting(team_id=team.id, title="t", transcript=secret)
        s.add(m)
        s.commit()
        meeting_id = m.id

    # Read raw column from SQLite — should be Fernet ciphertext, not plaintext.
    import sqlite3
    db_url = os.environ["LABFLOW_DATABASE_URL"]
    db_path = db_url.replace("sqlite:///", "")
    with sqlite3.connect(db_path) as conn:
        raw = conn.execute(
            "SELECT transcript FROM meetings WHERE id=?", (meeting_id,)
        ).fetchone()[0]
    assert secret not in raw
    assert raw.startswith("gAAAAA")

    # Read via SQLAlchemy — should be transparently decrypted.
    with SessionLocal() as s:
        m = s.get(models.Meeting, meeting_id)
        assert m.transcript == secret


# ---------------------------------------------------------------------------
# Plugins
# ---------------------------------------------------------------------------
def test_plugin_dotted_path_loads(monkeypatch):
    from labflow import plugins as plugins_mod
    plugins_mod.reset_registry_for_tests()
    monkeypatch.setenv("LABFLOW_PLUGINS", "tests.fixtures_plugin:plugin")
    reg = plugins_mod.get_registry()
    assert "noop" in reg.extractors
    plugins_mod.reset_registry_for_tests()


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------
def test_retention_sweep_drops_old_audit_rows(session, default_team):
    from datetime import timedelta

    from labflow import retention as retention_mod
    from labflow.time_utils import now_utc

    old = models.AuditEvent(
        team_id=default_team.id, action="meeting.finalized",
        entity_type="meeting", entity_id=1,
        created_at=(now_utc() - timedelta(days=400)).replace(tzinfo=None),
    )
    fresh = models.AuditEvent(
        team_id=default_team.id, action="task.status_changed",
        entity_type="task", entity_id=1,
    )
    session.add_all([old, fresh])
    session.flush()

    deleted = retention_mod.sweep(session, retention_mod.RetentionPolicy(
        audit_event_days=365,
    ))
    assert deleted["audit_events"] >= 1
    surviving_ids = [r.id for r in session.query(models.AuditEvent).all()]
    assert fresh.id in surviving_ids
    assert old.id not in surviving_ids


def test_export_team_includes_every_table(session, default_team):
    from labflow import retention as retention_mod
    m = models.Meeting(team_id=default_team.id, title="t",
                       transcript="@alice will write docs")
    session.add(m)
    session.flush()
    services.persist_extraction(session, m, extract(m.transcript))
    session.commit()

    snap = retention_mod.export_team(session, team_id=default_team.id)
    assert snap["team"]["slug"] == default_team.slug
    assert any(r["title"] == "t" for r in snap["meetings"])
    assert "tasks" in snap and "decisions" in snap and "embeddings" in snap


# ---------------------------------------------------------------------------
# /api/me endpoint (RBAC happy path)
# ---------------------------------------------------------------------------
def test_me_endpoint_returns_role(app_client):
    r = app_client.get("/api/me")
    assert r.status_code == 200
    body = r.json()
    assert "team" in body and "role" in body
    # Single-team mode → admin.
    assert body["role"] == "admin"


# ---------------------------------------------------------------------------
# Slack notifier reshape
# ---------------------------------------------------------------------------
def test_slack_payload_shape():
    from labflow.notifiers.slack import is_slack_url, to_slack_payload
    assert is_slack_url("https://hooks.slack.com/services/T/B/X")
    body = json.loads(to_slack_payload("meeting.finalized",
                                       {"meeting_id": 1, "title": "t"}))
    assert "blocks" in body
    assert any(b["type"] == "header" for b in body["blocks"])
