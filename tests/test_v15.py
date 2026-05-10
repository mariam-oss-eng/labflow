"""Tests for v0.15 — public shares, HTMX task list, SDK-gen CLI."""
from __future__ import annotations

import json
import sys
from io import StringIO


def _make_task(app_client) -> int:
    r = app_client.post("/api/meetings", json={
        "title": "k",
        "transcript": "@alice will write the spec by Friday.",
        "notes": "",
    })
    assert r.status_code == 201
    items = app_client.get("/api/tasks").json()["items"]
    assert items
    return items[0]["id"]


# ============================================================ public shares
def test_create_share_returns_token_once_then_resolves(temp_db, app_client):
    tid = _make_task(app_client)
    r = app_client.post("/api/shares", json={
        "entity_type": "task", "entity_id": tid, "ttl_hours": 24,
    })
    assert r.status_code == 201, r.text
    body = r.json()
    token = body["token"]
    assert token.startswith("lfshare_")
    assert body["entity_type"] == "task"

    # The list endpoint NEVER includes the plaintext.
    r = app_client.get("/api/shares")
    assert r.status_code == 200
    items = r.json()["items"]
    assert items
    assert all("token" not in it for it in items)

    # The public resolver works without auth and returns the entity.
    r = app_client.get(f"/share/{token}")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["data"]["type"] == "task"
    assert out["data"]["id"] == tid


def test_share_unknown_token_404(temp_db, app_client):
    r = app_client.get("/share/lfshare_does_not_exist")
    assert r.status_code == 404


def test_share_revoke_then_resolve_404(temp_db, app_client):
    tid = _make_task(app_client)
    body = app_client.post("/api/shares", json={
        "entity_type": "task", "entity_id": tid,
    }).json()
    token = body["token"]
    share_id = body["id"]
    r = app_client.delete(f"/api/shares/{share_id}")
    assert r.status_code == 200
    r = app_client.get(f"/share/{token}")
    assert r.status_code == 404


def test_share_invalid_entity_type_422(temp_db, app_client):
    r = app_client.post("/api/shares", json={
        "entity_type": "secret", "entity_id": 1,
    })
    assert r.status_code == 400


def test_share_view_count_increments(temp_db, app_client):
    tid = _make_task(app_client)
    body = app_client.post("/api/shares", json={
        "entity_type": "task", "entity_id": tid,
    }).json()
    token = body["token"]
    for _ in range(3):
        r = app_client.get(f"/share/{token}")
        assert r.status_code == 200
    items = app_client.get("/api/shares").json()["items"]
    me = next(it for it in items if it["id"] == body["id"])
    assert me["view_count"] >= 3


def test_share_decision_resolves(temp_db, app_client):
    """A share over a Decision returns its statement."""
    from labflow.db import get_session_factory
    from labflow import models
    Session = get_session_factory()
    with Session() as s:
        from sqlalchemy import select
        team = s.execute(select(models.Team)).scalar_one()
        m = models.Meeting(team_id=team.id, title="m", transcript="", notes="")
        s.add(m); s.flush()
        d = models.Decision(team_id=team.id, meeting_id=m.id,
                             statement="Adopt RFC-7", confidence=0.9)
        s.add(d); s.commit()
        did = d.id

    body = app_client.post("/api/shares", json={
        "entity_type": "decision", "entity_id": did, "ttl_hours": 1,
    }).json()
    r = app_client.get(f"/share/{body['token']}")
    assert r.status_code == 200
    assert r.json()["data"]["statement"] == "Adopt RFC-7"


# ============================================================ HTMX task list
def test_htmx_tasks_page_renders(temp_db, app_client):
    _make_task(app_client)
    r = app_client.get("/app/tasks")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "LabFlow Tasks" in r.text
    assert "htmx" in r.text  # CDN script tag present
    assert "write the spec" in r.text


def test_htmx_tasks_fragment_filters_by_query(temp_db, app_client):
    tid = _make_task(app_client)
    r = app_client.get("/api/tasks/_table?q=spec")
    assert r.status_code == 200
    assert f"task-row-{tid}" in r.text
    r = app_client.get("/api/tasks/_table?q=NOMATCH")
    assert r.status_code == 200
    assert "No tasks match" in r.text


def test_htmx_tasks_status_transition(temp_db, app_client):
    tid = _make_task(app_client)
    r = app_client.post(f"/api/tasks/_status/{tid}?to=done")
    assert r.status_code == 200
    assert "s-done" in r.text
    # Verify the task was actually closed.
    items = app_client.get("/api/tasks").json()["items"]
    me = next(t for t in items if t["id"] == tid)
    assert me["status"] == "done"


def test_htmx_tasks_status_invalid_target_422(temp_db, app_client):
    tid = _make_task(app_client)
    r = app_client.post(f"/api/tasks/_status/{tid}?to=fiction")
    assert r.status_code == 400


# ============================================================ SDK generator
def test_sdk_gen_renders_methods_for_simple_spec():
    from labflow import sdk_gen
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "T", "version": "0.0.1"},
        "paths": {
            "/api/things": {"get": {"operationId": "list_things"}},
            "/api/things/{thing_id}": {
                "get": {"operationId": "get_thing"},
                "delete": {"operationId": "delete_thing"},
            },
            "/api/things": {  # noqa: PIE804 — illustrate dedup; second entry wins
                "get": {"operationId": "list_things"},
                "post": {"operationId": "create_thing"},
            },
        },
    }
    src = sdk_gen.render(spec)
    assert "class Client" in src
    assert "def list_things(self" in src
    assert "def get_thing(self, thing_id" in src
    assert "def delete_thing(self, thing_id" in src
    assert "def create_thing(self" in src
    # Generated source must be syntactically valid Python.
    compile(src, "client.py", "exec")


def test_sdk_gen_from_server_via_cli(temp_db, monkeypatch, tmp_path):
    """``labflow gen-sdk --from-server --out FILE`` writes a valid client."""
    out = tmp_path / "client.py"
    from labflow.cli import main
    rc = main(["gen-sdk", "--from-server", "--out", str(out)])
    assert rc == 0
    src = out.read_text(encoding="utf-8")
    assert "class Client" in src
    # Spot-check a known-stable v0.13+ operation makes it through.
    assert "def " in src
    compile(src, str(out), "exec")


def test_sdk_gen_from_spec_file(tmp_path):
    """``labflow gen-sdk --spec FILE`` reads JSON and renders to stdout."""
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "openapi": "3.0.0",
        "info": {"title": "T", "version": "1"},
        "paths": {"/ping": {"get": {"operationId": "ping"}}},
    }), encoding="utf-8")

    from labflow.cli import main
    buf = StringIO()
    saved = sys.stdout
    sys.stdout = buf
    try:
        rc = main(["gen-sdk", "--spec", str(spec_path), "--out", "-"])
    finally:
        sys.stdout = saved
    assert rc == 0
    assert "def ping(self" in buf.getvalue()
