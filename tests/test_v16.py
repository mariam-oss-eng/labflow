"""Tests for v0.16 — LFQL, custom fields, scheduled reports."""
from __future__ import annotations


def _make_task(app_client, *, title="something") -> int:
    r = app_client.post("/api/meetings", json={
        "title": "k",
        "transcript": f"@alice will {title} by Friday.",
        "notes": "",
    })
    assert r.status_code == 201, r.text
    items = app_client.get("/api/tasks").json()["items"]
    return items[0]["id"]


# ============================================================ LFQL parser
def test_lfql_explain_parses_compound(temp_db, app_client):
    r = app_client.get(
        "/api/lfql/explain",
        params={"q": 'status:open AND owner:alice'},
    )
    assert r.status_code == 200, r.text
    ast = r.json()["ast"]
    assert "and" in ast
    leaves = [child["atom"] for child in ast["and"]]
    assert {"status", "owner"} == {leaf["key"] for leaf in leaves}


def test_lfql_explain_rejects_unknown_field(temp_db, app_client):
    r = app_client.get("/api/lfql/explain", params={"q": "nope:1"})
    assert r.status_code == 400


def test_lfql_run_filters_by_status(temp_db, app_client):
    _make_task(app_client, title="finish the report")
    r = app_client.get("/api/lfql/run",
                       params={"q": "status:open", "limit": 10})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matched"] >= 1
    assert all(it["status"] == "open" for it in body["items"])


def test_lfql_run_priority_compare(temp_db, app_client, session,
                                    default_team):
    from labflow import models
    _make_task(app_client, title="finish thing one")
    tid = app_client.get("/api/tasks").json()["items"][0]["id"]
    # Set priority directly (TaskUpdate schema is intentionally narrow).
    t = session.get(models.Task, tid)
    t.priority = "high"
    session.commit()

    r = app_client.get("/api/lfql/run",
                       params={"q": "priority:>=medium"})
    assert r.status_code == 200, r.text
    assert r.json()["matched"] == 1


def test_lfql_run_or_and_not(temp_db, app_client):
    _make_task(app_client, title="alpha")
    r = app_client.get("/api/lfql/run",
                       params={"q": "NOT status:done AND status:open"})
    assert r.status_code == 200
    assert r.json()["matched"] == 1


def test_lfql_quoted_string_with_spaces(temp_db, app_client):
    _make_task(app_client, title="login bug")
    r = app_client.get("/api/lfql/run",
                       params={"q": 'title:"login bug"'})
    assert r.status_code == 200
    assert r.json()["matched"] == 1


def test_lfql_invalid_query_400(temp_db, app_client):
    r = app_client.get("/api/lfql/run", params={"q": "status:"})
    assert r.status_code == 400


# ============================================================ custom fields
def test_custom_field_define_set_and_list(temp_db, app_client):
    tid = _make_task(app_client)
    r = app_client.post("/api/custom-fields", json={
        "entity_type": "task", "key": "epic", "label": "Epic",
        "kind": "select", "options": ["A", "B", "C"],
    })
    assert r.status_code == 201, r.text

    r = app_client.put(f"/api/task/{tid}/fields/epic",
                       json={"value": "B"})
    assert r.status_code == 200, r.text
    assert r.json()["values"] == {"epic": "B"}

    # Bad option rejected.
    r = app_client.put(f"/api/task/{tid}/fields/epic",
                       json={"value": "Z"})
    assert r.status_code == 400


def test_custom_field_number_coercion(temp_db, app_client):
    tid = _make_task(app_client)
    app_client.post("/api/custom-fields", json={
        "entity_type": "task", "key": "story_points",
        "label": "Story points", "kind": "number",
    })
    app_client.put(f"/api/task/{tid}/fields/story_points",
                   json={"value": "5"})
    body = app_client.get(f"/api/task/{tid}/fields").json()
    assert body["values"] == {"story_points": 5.0}


def test_custom_field_duplicate_define_409(temp_db, app_client):
    payload = {"entity_type": "task", "key": "k1",
               "label": "K1", "kind": "text"}
    assert app_client.post("/api/custom-fields", json=payload).status_code == 201
    assert app_client.post("/api/custom-fields", json=payload).status_code == 409


def test_custom_field_unset(temp_db, app_client):
    tid = _make_task(app_client)
    app_client.post("/api/custom-fields", json={
        "entity_type": "task", "key": "note", "label": "Note",
        "kind": "text",
    })
    app_client.put(f"/api/task/{tid}/fields/note", json={"value": "hi"})
    r = app_client.delete(f"/api/task/{tid}/fields/note")
    assert r.status_code == 200
    assert app_client.get(f"/api/task/{tid}/fields").json()["values"] == {}


# ============================================================ scheduled reports
def test_report_create_validates_query(temp_db, app_client):
    r = app_client.post("/api/reports", json={
        "name": "bad", "query": "garbage:::nope",
        "cadence": "daily",
        "webhook_url": "https://example/hook",
    })
    assert r.status_code == 400


def test_report_create_list_delete(temp_db, app_client):
    r = app_client.post("/api/reports", json={
        "name": "open-tasks-daily",
        "query": "status:open",
        "cadence": "daily",
        "webhook_url": "https://example/hook",
    })
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    items = app_client.get("/api/reports").json()["items"]
    assert any(it["id"] == rid for it in items)
    assert app_client.delete(f"/api/reports/{rid}").status_code == 200


def test_report_run_due_uses_lfql(temp_db, app_client, session, default_team):
    from datetime import datetime, timedelta

    from labflow import models, scheduled_reports
    _make_task(app_client)

    sess = session
    row = scheduled_reports.create(
        sess, team_id=default_team.id, name="open-now",
        query="status:open", cadence="hourly",
        webhook_url="https://example/h",
    )
    # Force it overdue.
    row.next_run_at = datetime.utcnow() - timedelta(minutes=1)
    sess.commit()

    posted = []

    def fake_post(url, body, headers):
        posted.append((url, body, headers))
        return 200, ""

    n = scheduled_reports.run_due(sess, http_post=fake_post)
    assert n == 1
    assert posted and posted[0][0] == "https://example/h"
    runs = scheduled_reports.runs_for(sess, team_id=default_team.id,
                                      report_id=row.id)
    assert runs and runs[0].delivered is True
    # next_run_at advanced.
    sess.refresh(row)
    assert row.next_run_at > datetime.utcnow()
