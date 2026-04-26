"""Tests for v0.6 (collaboration & insights) and v0.7 (realtime, GraphQL, observability)."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


# ============================================================ v0.6 collaboration
def _seed_meeting_with_decision_and_task(client: TestClient) -> tuple[int, int, int]:
    """Helper: create a meeting and return (meeting_id, decision_id, task_id)."""
    r = client.post("/api/meetings", json={
        "title": "v0.6 seed",
        "transcript": (
            "We decided to switch to Postgres for v2. "
            "TODO: provision the staging cluster by Friday. "
            "Owner: @alice"
        ),
        "notes": "",
    })
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    # Look up IDs directly (export.json doesn't include them).
    from labflow.db import get_session_factory
    from labflow import models
    with get_session_factory()() as s:
        d = s.query(models.Decision).filter_by(meeting_id=mid).first()
        t = s.query(models.Task).filter_by(meeting_id=mid).first()
        assert d is not None and t is not None
        return mid, d.id, t.id


def test_comments_thread(temp_db, app_client):
    mid, did, tid = _seed_meeting_with_decision_and_task(app_client)
    r = app_client.post("/api/comments", json={
        "entity_type": "task", "entity_id": tid, "body": "First!"
    })
    assert r.status_code == 201, r.text
    parent_id = r.json()["id"]
    r = app_client.post("/api/comments", json={
        "entity_type": "task", "entity_id": tid,
        "body": "Reply", "parent_id": parent_id,
    })
    assert r.status_code == 201
    r = app_client.get("/api/comments",
                       params={"entity_type": "task", "entity_id": tid})
    threaded = r.json()["comments"]
    assert len(threaded) == 1
    assert threaded[0]["body"] == "First!"
    assert len(threaded[0]["replies"]) == 1
    assert threaded[0]["replies"][0]["body"] == "Reply"


def test_comments_validation(temp_db, app_client):
    mid, _did, tid = _seed_meeting_with_decision_and_task(app_client)
    # empty body rejected
    r = app_client.post("/api/comments", json={
        "entity_type": "task", "entity_id": tid, "body": "  "
    })
    assert r.status_code == 400, r.text
    # unknown entity_type rejected
    r = app_client.post("/api/comments", json={
        "entity_type": "owner", "entity_id": tid, "body": "x"
    })
    assert r.status_code == 400


def test_comment_soft_delete(temp_db, app_client):
    _, _, tid = _seed_meeting_with_decision_and_task(app_client)
    r = app_client.post("/api/comments", json={
        "entity_type": "task", "entity_id": tid, "body": "transient"
    })
    cid = r.json()["id"]
    r = app_client.delete(f"/api/comments/{cid}")
    assert r.status_code == 200
    r = app_client.get("/api/comments",
                       params={"entity_type": "task", "entity_id": tid})
    # soft-deleted rows are filtered out
    assert r.json()["comments"] == []


def test_reactions_toggle(temp_db, app_client):
    _, did, _ = _seed_meeting_with_decision_and_task(app_client)
    r = app_client.post("/api/reactions", json={
        "entity_type": "decision", "entity_id": did, "emoji": "👍"
    })
    assert r.status_code == 200, r.text
    assert r.json()["added"] is True
    assert r.json()["counts"]["👍"] == 1
    # toggle off
    r = app_client.post("/api/reactions", json={
        "entity_type": "decision", "entity_id": did, "emoji": "👍"
    })
    assert r.json()["added"] is False
    assert r.json()["counts"].get("👍", 0) == 0


def test_reactions_emoji_validation(temp_db, app_client):
    _, _, tid = _seed_meeting_with_decision_and_task(app_client)
    r = app_client.post("/api/reactions", json={
        "entity_type": "task", "entity_id": tid, "emoji": "🥑"
    })
    assert r.status_code == 400


# ----------------------------------------------------------- saved searches
def test_saved_search_crud_and_run(temp_db, app_client):
    _seed_meeting_with_decision_and_task(app_client)
    r = app_client.post("/api/saved-searches", json={
        "name": "Postgres mentions", "query": "postgres",
        "alpha": 0.3, "pinned": True,
    })
    assert r.status_code == 201, r.text
    slug = r.json()["slug"]
    assert slug == "postgres-mentions"
    # list
    r = app_client.get("/api/saved-searches")
    items = r.json()["items"]
    assert len(items) == 1 and items[0]["pinned"] is True
    # run
    r = app_client.get(f"/api/saved-searches/{slug}/run")
    assert r.status_code == 200
    data = r.json()
    assert data["saved_search"]["query"] == "postgres"
    assert isinstance(data["results"], list)
    # delete
    r = app_client.delete(f"/api/saved-searches/{slug}")
    assert r.status_code == 200
    r = app_client.get("/api/saved-searches")
    assert r.json()["items"] == []


def test_saved_search_duplicate_slug_409(temp_db, app_client):
    _seed_meeting_with_decision_and_task(app_client)
    r = app_client.post("/api/saved-searches", json={
        "name": "Foo", "query": "foo"
    })
    assert r.status_code == 201
    r = app_client.post("/api/saved-searches", json={
        "name": "Foo", "query": "foo"
    })
    assert r.status_code == 409


# ----------------------------------------------------------- analytics
def test_analytics_basic(temp_db, app_client):
    _, _, tid = _seed_meeting_with_decision_and_task(app_client)
    # close the task so cycle time has a sample
    r = app_client.patch(f"/api/tasks/{tid}", json={"status": "done"})
    assert r.status_code == 200
    r = app_client.get("/api/analytics", params={"days": 30})
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["meetings"] >= 1
    assert a["tasks_opened"] >= 1
    assert a["tasks_closed"] >= 1
    assert a["completion_rate"] > 0
    assert isinstance(a["weekly_trend"], list) and len(a["weekly_trend"]) == 8
    # cycle time should be a small non-negative number
    assert a["cycle_time_p50_days"] is not None
    assert a["cycle_time_p50_days"] >= 0


# ----------------------------------------------------------- iCalendar feed
def test_calendar_ics_feed(temp_db, app_client):
    # seed a meeting with a due date task
    r = app_client.post("/api/meetings", json={
        "title": "Plan",
        "transcript": (
            "TODO: deploy the staging cluster by 2026-12-15. Owner: @alice"
        ),
        "notes": "",
    })
    assert r.status_code == 201
    r = app_client.get("/api/calendar.ics")
    assert r.status_code == 200
    ct = r.headers["content-type"]
    assert ct.startswith("text/calendar")
    body = r.text
    assert body.startswith("BEGIN:VCALENDAR")
    assert "BEGIN:VEVENT" in body
    assert "END:VCALENDAR" in body
    assert "deploy the staging cluster" in body or "[LabFlow]" in body


def test_calendar_ics_escapes_special_chars(temp_db, session, default_team):
    from labflow import calendar_feed, models
    from datetime import datetime, timedelta, timezone

    t = models.Task(
        team_id=default_team.id,
        meeting_id=0,
        title="Title, with; special\\chars\nand\nnewline",
        description="Body, also; tricky",
        due_date=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1),
        confidence=0.9, kind="task", status="open",
    )
    # create a host meeting first to satisfy FK
    m = models.Meeting(team_id=default_team.id, title="m")
    session.add(m); session.flush()
    t.meeting_id = m.id
    session.add(t); session.commit()
    body = calendar_feed.render_team_calendar(session, team_id=default_team.id)
    assert "\\," in body and "\\;" in body and "\\\\" in body
    assert "\\n" in body  # the newline got escaped


# ----------------------------------------------------------- AI summary
def test_summary_offline_textrank(temp_db, app_client):
    transcript = (
        "We met to discuss the v2 architecture migration. "
        "The team agreed Postgres is the right target for OLTP workloads. "
        "Alice will provision the staging cluster by Friday. "
        "Bob raised a concern about the embedding store memory footprint. "
        "We decided to use the existing JSON column approach for now. "
        "Charlie will benchmark search latency next week. "
        "If results are bad, we will revisit the choice in two weeks."
    )
    r = app_client.post("/api/meetings", json={
        "title": "v2 plan", "transcript": transcript, "notes": "",
    })
    assert r.status_code == 201
    mid = r.json()["id"]
    r = app_client.get(f"/api/meetings/{mid}/summary",
                       params={"max_sentences": 3})
    assert r.status_code == 200
    j = r.json()
    assert j["backend"] == "textrank"
    assert isinstance(j["summary"], str) and len(j["summary"]) > 20
    # summary should not be longer than the original
    assert len(j["summary"]) < len(transcript)


def test_summary_callable_hook(temp_db, app_client, monkeypatch):
    # Point the env at a stub callable; settings reads on first access in
    # summarize() — for this test we monkeypatch the resolver directly.
    from labflow import summary as sm
    monkeypatch.setattr(sm, "_resolve_callable", lambda p: (lambda text, *, max_sentences: "STUB SUMMARY"))
    monkeypatch.setattr(sm, "get_settings", lambda: type("S", (), {"summary_callable": "x:y"})())
    out = sm.summarize("anything at all here", max_sentences=2)
    assert out == {"summary": "STUB SUMMARY", "backend": "x:y"}


# ----------------------------------------------------------- HTML digest
def test_digest_html_contains_summary(temp_db, app_client):
    _seed_meeting_with_decision_and_task(app_client)
    r = app_client.get("/api/digest/weekly.html")
    assert r.status_code == 200
    body = r.text
    assert "<h1" in body and "LabFlow Weekly Digest" in body
    # inline style should be present (no external CSS dep)
    assert "style=" in body


# ============================================================ v0.7 graphql / ws
def test_graphql_basic_query(temp_db, app_client):
    _, _, _ = _seed_meeting_with_decision_and_task(app_client)
    q = "{ team { id slug } tasks(limit: 5) { id title status } analytics(days: 30) { tasks_opened } }"
    r = app_client.post("/graphql", json={"query": q})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["team"]["slug"] == "default"
    assert isinstance(data["tasks"], list)
    assert data["analytics"]["tasks_opened"] >= 1


def test_graphql_introspection_schema(temp_db, app_client):
    r = app_client.post("/graphql", json={"query": "{ __schema { types { name } } }"})
    assert r.status_code == 200
    types = r.json()["data"]["__schema"]["types"]
    names = [t["name"] for t in types]
    assert "Query" in names and "Task" in names and "Decision" in names


def test_graphql_unknown_field_reports_error(temp_db, app_client):
    r = app_client.post("/graphql", json={"query": "{ bogus { id } }"})
    assert r.status_code == 200
    body = r.json()
    assert body["data"] == {} or "bogus" not in body["data"]
    assert any("bogus" in e["message"] for e in body["errors"])


def test_websocket_hello_and_event(temp_db, app_client):
    """WebSocket connects, gets hello, receives a published event."""
    with app_client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "hello"
        team_id = msg["team_id"]
        # Publish from server side and expect to see it
        from labflow import sse as sse_mod
        sse_mod.hub().publish(team_id, "task.closed", {"task_id": 99})
        msg = ws.receive_json()
        assert msg["type"] == "event"
        assert msg["event"] == "task.closed"
        assert msg["data"]["task_id"] == 99


def test_websocket_subscribe_filters(temp_db, app_client):
    """Client subscribes to a single event; unmatched events are skipped."""
    with app_client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        team_id = hello["team_id"]
        ws.send_json({"type": "subscribe", "events": ["task.closed"]})
        from labflow import sse as sse_mod
        sse_mod.hub().publish(team_id, "meeting.finalized", {"id": 1})
        sse_mod.hub().publish(team_id, "task.closed", {"id": 2})
        msg = ws.receive_json()
        assert msg["event"] == "task.closed"


def test_websocket_ping_pong(temp_db, app_client):
    with app_client.websocket_connect("/ws") as ws:
        ws.receive_json()  # hello
        ws.send_json({"type": "ping", "ts": 42})
        msg = ws.receive_json()
        assert msg["type"] == "pong" and msg["ts"] == 42


# ----------------------------------------------------------- worker lock
def test_worker_lock_acquire_release(temp_db, session, default_team):
    from labflow import worker_lock as wl
    # First owner wins
    a = wl.acquire(session, name="job-runner", owner="A", ttl_seconds=60)
    assert a == "A"
    # Second owner is blocked while lease is fresh
    b = wl.acquire(session, name="job-runner", owner="B", ttl_seconds=60)
    assert b is None
    # Heartbeat works
    assert wl.heartbeat(session, name="job-runner", owner="A") is True
    # Release lets B acquire
    wl.release(session, name="job-runner", owner="A")
    b2 = wl.acquire(session, name="job-runner", owner="B", ttl_seconds=60)
    assert b2 == "B"


def test_worker_lock_steal_after_expiry(temp_db, session, default_team):
    from labflow import worker_lock as wl, models
    from datetime import datetime, timedelta, timezone
    a = wl.acquire(session, name="lease", owner="A", ttl_seconds=60)
    assert a == "A"
    # Expire the lease in the past
    row = session.get(models.WorkerLock, "lease")
    row.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    session.commit()
    b = wl.acquire(session, name="lease", owner="B", ttl_seconds=60)
    assert b == "B"


# ----------------------------------------------------------- otel no-op
def test_otel_disabled_by_default():
    from labflow import otel
    # without env var, setup() returns False and span() is a no-op
    assert otel.setup() is False
    assert otel.is_enabled() is False
    with otel.span("test", k=1) as sp:
        assert sp is None


# ----------------------------------------------------------- pg_fts non-postgres
def test_pg_fts_returns_none_on_sqlite(temp_db, session, default_team):
    from labflow import pg_fts
    out = pg_fts.maybe_pg_search(session, team_id=default_team.id, tokens=["foo"])
    assert out is None


# ----------------------------------------------------------- Python SDK
def test_python_sdk_against_test_client(temp_db, app_client):
    """The SDK uses urllib by default; here we monkeypatch its transport
    to dispatch through the FastAPI TestClient so we don't need a real
    HTTP listener inside the test runner."""
    from labflow_client import LabFlow

    class _Stub(LabFlow):
        def _request(self, method, path, *, params=None, json_body=None,
                     idempotency_key=None):
            from urllib.parse import urlencode
            url = path
            if params:
                qs = urlencode({k: v for k, v in params.items() if v is not None})
                if qs:
                    url = f"{path}?{qs}"
            headers = {}
            if idempotency_key:
                headers["Idempotency-Key"] = idempotency_key
            resp = app_client.request(method, url, json=json_body, headers=headers)
            if resp.status_code >= 400:
                from labflow_client import LabFlowError
                raise LabFlowError(resp.status_code, resp.text, url=path)
            if not resp.content:
                return None
            ctype = resp.headers.get("content-type", "")
            return resp.json() if "json" in ctype else resp.text

    sdk = _Stub("http://test", api_key=None)
    assert sdk.health()["ok"] is True
    m = sdk.create_meeting(title="SDK", transcript="TODO: ship it. Owner: @alice")
    assert m["id"] >= 1
    tasks = sdk.list_tasks()
    assert "items" in tasks
    out = sdk.search("ship")
    assert "results" in out
    g = sdk.graphql("{ team { slug } }")
    assert g["data"]["team"]["slug"] == "default"


# ----------------------------------------------------------- saved search slugify
def test_slugify_helper():
    from labflow.saved_searches import slugify
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("café au lait") == "cafe-au-lait"
    assert slugify("") == "search"
    assert len(slugify("x" * 200)) == 64


# ----------------------------------------------------------- analytics edge case
def test_analytics_zero_meetings(temp_db, session, default_team):
    from labflow import analytics
    a = analytics.compute(session, team_id=default_team.id, days=7)
    assert a.tasks_opened == 0
    assert a.completion_rate == 0.0
    assert a.cycle_time_p50_days is None
