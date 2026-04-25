"""Tests for the v0.3 endpoints: search, async extract, jobs, metrics, GitHub webhook."""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest


def _seed(client):
    return client.post("/api/meetings", json={
        "title": "m1",
        "transcript": (
            "@alice will retrain the tokenizer model tomorrow. "
            "We decided to use SentencePiece because it's faster. "
            "@bob will write the eval harness next week."
        ),
    }).json()


# --- search ----------------------------------------------------------------
def test_search_finds_decision_by_keyword(app_client):
    _seed(app_client)
    body = app_client.get("/api/search?q=sentencepiece").json()
    assert body["query"] == "sentencepiece"
    kinds = {h["kind"] for h in body["results"]}
    assert "decision" in kinds


def test_search_finds_task_by_keyword(app_client):
    _seed(app_client)
    body = app_client.get("/api/search?q=tokenizer").json()
    titles = [h["title"].lower() for h in body["results"]]
    assert any("tokenizer" in t for t in titles)


def test_search_empty_query(app_client):
    _seed(app_client)
    body = app_client.get("/api/search?q=").json()
    assert body["results"] == []


def test_search_no_match(app_client):
    _seed(app_client)
    body = app_client.get("/api/search?q=quantumcomputingunicorn").json()
    assert body["results"] == []


# --- async extract + jobs --------------------------------------------------
def test_async_extract_enqueues_job_and_runs(app_client):
    m = _seed(app_client)
    r = app_client.post(f"/api/meetings/{m['id']}/extract:async")
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert r.json()["status"] == "queued"

    # Drain the queue.
    from labflow import jobs as jobs_mod
    while jobs_mod.run_once():
        pass

    body = app_client.get(f"/api/jobs/{job_id}").json()
    assert body["status"] == "completed"
    assert body["result"]["meeting_id"] == m["id"]


def test_async_extract_idempotent(app_client):
    m = _seed(app_client)
    r1 = app_client.post(f"/api/meetings/{m['id']}/extract:async").json()
    r2 = app_client.post(f"/api/meetings/{m['id']}/extract:async").json()
    assert r1["job_id"] == r2["job_id"]


def test_async_extract_finalized_returns_409(app_client):
    m = _seed(app_client)
    app_client.post(f"/api/meetings/{m['id']}/finalize")
    r = app_client.post(f"/api/meetings/{m['id']}/extract:async")
    assert r.status_code == 409


def test_get_unknown_job_404(app_client):
    r = app_client.get("/api/jobs/9999")
    assert r.status_code == 404


# --- audit log -------------------------------------------------------------
def test_finalize_writes_audit_event(app_client, session, default_team):
    from labflow import models
    m = _seed(app_client)
    app_client.post(f"/api/meetings/{m['id']}/finalize")
    rows = session.query(models.AuditEvent).filter(
        models.AuditEvent.action == "meeting.finalized"
    ).all()
    assert any(r.entity_id == m["id"] for r in rows)


def test_evidence_verified_writes_audit_event(app_client, session):
    from labflow import models
    m = _seed(app_client)
    tasks = app_client.get("/api/tasks").json()["items"]
    target = next(t for t in tasks if "tokenizer" in t["title"].lower())
    app_client.post("/api/evidence", json={
        "task_id": target["id"],
        "kind": "commit",
        "uri": "https://github.com/lab/repo/commit/abc",
        "summary": "alice retrain tokenizer model",
    })
    actions = {r.action for r in session.query(models.AuditEvent).all()}
    assert "evidence.verified" in actions


# --- webhooks --------------------------------------------------------------
def test_outbound_webhook_emit_and_deliver(temp_db, session, default_team, monkeypatch):
    """Outbound webhooks are queued on emit() and delivered by deliver_pending()."""
    from labflow import models, webhooks
    from labflow.time_utils import now_utc

    # Subscribe to all events.
    sub = models.WebhookSubscription(
        team_id=default_team.id, url="https://example.test/hook",
        event="*", secret="topsecret", active=True, created_at=now_utc(),
    )
    session.add(sub)
    session.flush()

    webhooks.emit(session, team_id=default_team.id, event="meeting.finalized",
                  payload={"meeting_id": 42})

    # Deliveries are queued, none successful yet.
    deliveries = session.query(models.WebhookDelivery).all()
    assert len(deliveries) == 1
    assert deliveries[0].success is False

    # Inject a fake HTTP poster.
    captured = {}
    def fake_post(url, body, headers):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return 200, "ok"

    n = webhooks.deliver_pending(session, http_post=fake_post)
    session.commit()
    assert n == 1
    refreshed = session.query(models.WebhookDelivery).first()
    assert refreshed.success is True
    assert refreshed.status_code == 200

    # Signature should verify.
    sig = captured["headers"]["X-LabFlow-Signature-256"]
    expected = "sha256=" + hmac.new(b"topsecret", captured["body"],
                                    hashlib.sha256).hexdigest()
    assert sig == expected


def test_webhook_delivery_retries_on_failure(temp_db, session, default_team):
    from labflow import models, webhooks
    from labflow.time_utils import now_utc

    sub = models.WebhookSubscription(
        team_id=default_team.id, url="https://example.test/hook",
        event="*", secret="s", active=True, created_at=now_utc(),
    )
    session.add(sub)
    session.flush()
    webhooks.emit(session, team_id=default_team.id, event="x", payload={})

    def boom(url, body, headers):
        raise RuntimeError("connection refused")

    webhooks.deliver_pending(session, http_post=boom)
    d = session.query(models.WebhookDelivery).first()
    assert d.attempts == 1
    assert d.success is False
    # Retries up to the cap (5).
    for _ in range(10):
        webhooks.deliver_pending(session, http_post=boom)
    session.refresh(d)
    assert d.attempts == 5  # capped


def test_inbound_github_webhook_invalid_signature(app_client, monkeypatch):
    from labflow import config as config_mod
    monkeypatch.setenv("LABFLOW_GITHUB_WEBHOOK_SECRET", "abc")
    config_mod.reset_settings_cache()
    r = app_client.post("/api/webhooks/github",
                        content=b'{"foo":1}',
                        headers={"X-Hub-Signature-256": "sha256=deadbeef",
                                 "X-GitHub-Event": "push"})
    assert r.status_code == 401


def test_inbound_github_webhook_valid_signature(app_client, monkeypatch):
    from labflow import config as config_mod
    secret = "abc"
    monkeypatch.setenv("LABFLOW_GITHUB_WEBHOOK_SECRET", secret)
    config_mod.reset_settings_cache()

    body = json.dumps({"commits": [], "pull_request": {}}).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    r = app_client.post("/api/webhooks/github", content=body,
                        headers={"X-Hub-Signature-256": sig,
                                 "X-GitHub-Event": "ping",
                                 "Content-Type": "application/json"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


# --- metrics ---------------------------------------------------------------
def test_metrics_endpoint_renders_text(app_client):
    _seed(app_client)
    r = app_client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    assert "# HELP" in body
    assert "labflow_jobs" in body or "labflow_audit_events_total" in body


def test_metrics_inc_and_observe():
    from labflow import metrics as m
    m.inc("test_counter", 2.0, source="unit")
    m.inc("test_counter", 1.0, source="unit")
    m.observe("test_summary", 0.123)
    out = m.render(None)
    assert 'test_counter{source="unit"} 3.0' in out
    assert "test_summary_count" in out
    assert "test_summary_sum" in out
