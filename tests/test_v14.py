"""Tests for v0.14 — time tracking, feature flags, smart-list subscriptions, MCP."""
from __future__ import annotations

from datetime import datetime, timedelta


def _make_task(app_client) -> int:
    r = app_client.post("/api/meetings", json={
        "title": "k",
        "transcript": "@alice will write the spec by Friday.",
        "notes": "",
    })
    assert r.status_code == 201
    r = app_client.get("/api/tasks")
    assert r.status_code == 200
    items = r.json()["items"]
    assert items
    return items[0]["id"]


# ============================================================ time tracking
def test_time_start_and_stop_timer(temp_db, app_client):
    tid = _make_task(app_client)
    r = app_client.post(f"/api/tasks/{tid}/time/start", json={})
    assert r.status_code == 200, r.text
    assert r.json()["task_id"] == tid

    r = app_client.post("/api/tasks/time/stop", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_id"] == tid
    assert body["ended_at"] is not None


def test_time_start_replaces_open_timer(temp_db, app_client):
    """Starting a 2nd timer for the same owner closes the first one."""
    tid1 = _make_task(app_client)
    # Make a 2nd task in same team via raw SQL so we don't depend on extraction.
    from labflow.db import get_session_factory
    from labflow import models
    Session = get_session_factory()
    with Session() as s:
        t1 = s.get(models.Task, tid1)
        t2 = models.Task(team_id=t1.team_id, meeting_id=t1.meeting_id,
                         title="task two", status="open", state="todo")
        s.add(t2); s.commit()
        tid2 = t2.id

    r = app_client.post(f"/api/tasks/{tid1}/time/start", json={})
    assert r.status_code == 200
    r = app_client.post(f"/api/tasks/{tid2}/time/start", json={})
    assert r.status_code == 200

    # Only one open timer should remain.
    with Session() as s:
        from sqlalchemy import select
        open_rows = list(s.execute(
            select(models.TimeEntry).where(models.TimeEntry.ended_at.is_(None))
        ).scalars().all())
        assert len(open_rows) == 1
        assert open_rows[0].task_id == tid2


def test_time_log_manual_and_summary(temp_db, app_client):
    tid = _make_task(app_client)
    start = (datetime.utcnow() - timedelta(hours=2)).isoformat()
    end = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    r = app_client.post(f"/api/tasks/{tid}/time", json={
        "started_at": start, "ended_at": end, "note": "deep work",
    })
    assert r.status_code == 200, r.text

    r = app_client.get(f"/api/tasks/{tid}/time")
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == tid
    assert 3500 <= body["total_seconds"] <= 3700  # ~1h
    assert body["total_hours"] >= 0.97


def test_time_manual_rejects_inverted_range(temp_db, app_client):
    tid = _make_task(app_client)
    end = (datetime.utcnow() - timedelta(hours=2)).isoformat()
    start = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    r = app_client.post(f"/api/tasks/{tid}/time", json={
        "started_at": start, "ended_at": end,
    })
    assert r.status_code == 400


def test_time_stop_without_running_409(temp_db, app_client):
    _make_task(app_client)
    r = app_client.post("/api/tasks/time/stop", json={})
    assert r.status_code == 409


def test_time_team_report(temp_db, app_client):
    tid = _make_task(app_client)
    start = (datetime.utcnow() - timedelta(hours=2)).isoformat()
    end = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    r = app_client.post(f"/api/tasks/{tid}/time", json={
        "started_at": start, "ended_at": end,
    })
    assert r.status_code == 200
    r = app_client.get("/api/time/report?days=7")
    assert r.status_code == 200
    body = r.json()
    assert body["entry_count"] >= 1
    assert any(row["task_id"] == tid for row in body["by_task"])


def test_task_effort_endpoint(temp_db, app_client):
    tid = _make_task(app_client)
    r = app_client.put(f"/api/tasks/{tid}/effort", json={"effort_hours": 6.5})
    assert r.status_code == 200, r.text
    assert r.json()["effort_hours"] == 6.5
    r = app_client.put(f"/api/tasks/{tid}/effort", json={"effort_hours": -1})
    assert r.status_code == 400


# ============================================================ feature flags
def test_feature_flags_upsert_list_delete(temp_db, app_client):
    r = app_client.put("/api/feature-flags/copilot.beta",
                       json={"enabled": True, "payload": {"variant": "B"}})
    assert r.status_code == 200, r.text
    r = app_client.get("/api/feature-flags")
    assert r.status_code == 200
    items = r.json()["items"]
    keys = {i["key"]: i for i in items}
    assert "copilot.beta" in keys
    assert keys["copilot.beta"]["enabled"] is True

    r = app_client.delete("/api/feature-flags/copilot.beta")
    assert r.status_code == 200
    r = app_client.get("/api/feature-flags")
    assert all(i["key"] != "copilot.beta" for i in r.json()["items"])


def test_feature_flags_invalid_key_rejected(temp_db, app_client):
    r = app_client.put("/api/feature-flags/bad key with spaces!",
                       json={"enabled": True})
    assert r.status_code == 400


def test_feature_flags_helper_uses_cache(temp_db, default_team, session):
    """``is_enabled`` returns the live value and caches it."""
    from labflow import feature_flags as ff
    ff.reset_cache()
    assert ff.is_enabled(session, team_id=default_team.id, key="x") is False
    ff.upsert(session, team_id=default_team.id, key="x", enabled=True)
    assert ff.is_enabled(session, team_id=default_team.id, key="x") is True


# ============================================================ smart-list subs
def _create_smart_list(app_client) -> str:
    r = app_client.put("/api/smart-lists/open-tasks", json={
        "name": "Open tasks",
        "filter": {"status": "open"},
    })
    assert r.status_code in (200, 201), r.text
    return "open-tasks"


def test_smart_list_subscribe_and_sweep(temp_db, app_client):
    _make_task(app_client)
    slug = _create_smart_list(app_client)

    r = app_client.post(f"/api/smart-lists/{slug}/subscriptions", json={
        "webhook_url": "https://example.com/hook", "secret": "shh",
    })
    assert r.status_code == 200, r.text
    sub_id = r.json()["id"]

    # First sweep — digest is None → fires.
    r = app_client.post("/api/admin/smart-lists/sweep")
    assert r.status_code == 200
    fired = r.json()["fired"]
    assert any(f["subscription_id"] == sub_id for f in fired)

    # Second sweep — same task set → no fire.
    r = app_client.post("/api/admin/smart-lists/sweep")
    assert r.status_code == 200
    assert r.json()["fired"] == []

    r = app_client.delete(f"/api/smart-lists/subscriptions/{sub_id}")
    assert r.status_code == 200


def test_smart_list_subscribe_rejects_non_http_url(temp_db, app_client):
    _create_smart_list(app_client)
    r = app_client.post("/api/smart-lists/open-tasks/subscriptions",
                        json={"webhook_url": "ftp://example.com/x"})
    assert r.status_code == 400


# ============================================================ MCP JSON-RPC
def test_mcp_tools_list(temp_db, app_client):
    r = app_client.get("/api/mcp/tools")
    assert r.status_code == 200, r.text
    names = {t["name"] for t in r.json()["tools"]}
    assert {"search", "list_open_tasks", "get_task",
            "list_decisions", "analytics"}.issubset(names)


def test_mcp_jsonrpc_call_list_open_tasks(temp_db, app_client):
    _make_task(app_client)
    r = app_client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "list_open_tasks", "arguments": {"limit": 5}},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["jsonrpc"] == "2.0" and body["id"] == 1
    assert body["result"]["isError"] is False
    inner = body["result"]["content"][0]["json"]
    assert isinstance(inner["tasks"], list)


def test_mcp_jsonrpc_unknown_method(temp_db, app_client):
    r = app_client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 7, "method": "bogus/method",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["error"]["code"] == -32601


def test_mcp_jsonrpc_unknown_tool(temp_db, app_client):
    r = app_client.post("/api/mcp", json={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": "no-such-tool"},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["isError"] is True
