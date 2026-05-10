"""Tests for v0.13 — federated guest invites, smart lists, markdown
bundle export, interactive REPL.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime


# =================================================================== invites
def test_invite_lifecycle_create_accept(temp_db, default_team, app_client):
    # 1. Admin creates an invite scoped to a single task.
    # First seed a task we can ACL-share.
    r = app_client.post("/api/meetings", json={
        "title": "seed",
        "transcript": "@alice will do thing by Friday.",
        "notes": "",
    })
    assert r.status_code == 201
    task_id = app_client.get("/api/tasks").json()["items"][0]["id"]

    r = app_client.post("/api/invites", json={
        "email": "guest@example.com",
        "role": "viewer",
        "acl_entries": [
            {"entity_type": "task", "entity_id": task_id, "permission": "read"},
        ],
        "ttl_hours": 24,
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == "guest@example.com"
    token = body["token"]
    assert len(token) > 30

    # 2. Listing shows it as pending.
    r = app_client.get("/api/invites?status=pending")
    items = r.json()["items"]
    assert any(i["email"] == "guest@example.com" for i in items)

    # 3. Accept exchanges the token for an API key + writes ACL row.
    r = app_client.post("/api/invites/accept", json={"token": token})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["api_key"].startswith("lf_") or len(body["api_key"]) > 30
    assert body["team_id"] == default_team.id

    # 4. ACL row exists for the new guest key.
    from labflow import models
    SessionLocal = temp_db.get_session_factory()
    with SessionLocal() as s:
        acls = s.query(models.ResourceAcl).filter_by(
            team_id=default_team.id, entity_id=task_id,
            api_key_id=body["api_key_id"],
        ).all()
        assert len(acls) == 1 and acls[0].permission == "read"


def test_invite_double_accept_rejected(temp_db, default_team, app_client):
    r = app_client.post("/api/invites", json={
        "email": "g2@example.com", "role": "viewer",
    })
    assert r.status_code == 201
    token = r.json()["token"]
    r = app_client.post("/api/invites/accept", json={"token": token})
    assert r.status_code == 200
    r = app_client.post("/api/invites/accept", json={"token": token})
    assert r.status_code == 400  # already accepted


def test_invite_revoke(temp_db, default_team, app_client):
    r = app_client.post("/api/invites", json={
        "email": "g3@example.com", "role": "viewer",
    })
    iid = r.json()["id"]
    token = r.json()["token"]
    r = app_client.delete(f"/api/invites/{iid}")
    assert r.status_code == 200
    # Now accept fails.
    r = app_client.post("/api/invites/accept", json={"token": token})
    assert r.status_code == 400


def test_invite_acl_validation(temp_db, app_client):
    r = app_client.post("/api/invites", json={
        "email": "g@example.com",
        "acl_entries": [
            {"entity_type": "blackhole", "entity_id": 1, "permission": "read"},
        ],
    })
    assert r.status_code == 400


# ============================================================== smart lists
def test_smart_list_filter_by_priority(temp_db, app_client):
    # Seed a couple tasks.
    app_client.post("/api/meetings", json={
        "title": "s1",
        "transcript": "@alice will ship A by Friday.",
        "notes": "",
    })
    app_client.post("/api/meetings", json={
        "title": "s2",
        "transcript": "@bob will ship B by Friday.",
        "notes": "",
    })
    ids = [t["id"] for t in app_client.get("/api/tasks").json()["items"]]
    # Mark first as high priority via bulk.
    app_client.post("/api/tasks/bulk", json={
        "op": "set_priority",
        "task_ids": [ids[0]],
        "args": {"priority": "high"},
    })

    r = app_client.put("/api/smart-lists/hot", json={
        "name": "Hot tasks",
        "filter": {"priority": "high"},
    })
    assert r.status_code == 200, r.text
    r = app_client.get("/api/smart-lists/hot/run")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 1 and body["items"][0]["id"] == ids[0]


def test_smart_list_filter_by_assignee_and_label(temp_db, app_client):
    app_client.post("/api/meetings", json={
        "title": "x",
        "transcript": "@alice will deploy staging by Friday. "
                      "@alice will write the design doc.",
        "notes": "",
    })
    r = app_client.put("/api/smart-lists/alice-deploy", json={
        "name": "alice-deploy",
        "filter": {"assignee_handle": "alice", "label": "deploy"},
    })
    assert r.status_code == 200
    r = app_client.get("/api/smart-lists/alice-deploy/run")
    items = r.json()["items"]
    assert len(items) == 1
    assert "deploy" in items[0]["title"].lower()


def test_smart_list_invalid_filter_key(temp_db, app_client):
    r = app_client.put("/api/smart-lists/bad", json={
        "name": "bad",
        "filter": {"colour": "red"},
    })
    assert r.status_code == 400


def test_smart_list_listing_and_delete(temp_db, app_client):
    app_client.put("/api/smart-lists/x", json={
        "name": "X", "filter": {"status": "open"},
    })
    r = app_client.get("/api/smart-lists")
    slugs = [it["slug"] for it in r.json()["items"]]
    assert "x" in slugs
    r = app_client.delete("/api/smart-lists/x")
    assert r.status_code == 200
    r = app_client.get("/api/smart-lists/x/run")
    assert r.status_code == 404


# ============================================================ bundle export
def test_bundle_zip_contains_expected_entries(temp_db, default_team, app_client):
    # Seed: one meeting → one decision + one task; one wiki page.
    r = app_client.post("/api/meetings", json={
        "title": "bundle-meeting",
        "transcript": "We decided to ship v1. "
                      "@alice will deploy by Monday.",
        "notes": "",
    })
    assert r.status_code == 201
    r = app_client.post("/api/wiki/pages", json={
        "title": "Onboarding",
        "body": "Welcome to the team.",
    })
    assert r.status_code == 201

    r = app_client.get("/api/admin/export/bundle.zip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert "manifest.json" in names
    assert "audit.jsonl" in names
    assert any(n.startswith("meetings/") and n.endswith(".md") for n in names)
    assert any(n.startswith("wiki/") and n.endswith(".md") for n in names)

    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["team"]["slug"] == default_team.slug
    assert manifest["counts"]["meetings"] >= 1
    assert manifest["counts"]["wiki_pages"] >= 1


def test_bundle_audit_jsonl_lines_parse(temp_db, default_team, app_client):
    # Touching wiki produces audit rows even if extractor finds nothing.
    app_client.post("/api/wiki/pages", json={
        "title": "Audit Bundle Test",
        "body": "hello",
    })
    r = app_client.get("/api/admin/export/bundle.zip")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    audit = zf.read("audit.jsonl").decode("utf-8").strip().splitlines()
    assert audit, "expected at least one audit line"
    for line in audit:
        row = json.loads(line)
        assert "action" in row and "entry_hash" in row


# ====================================================================== REPL
def test_repl_runs_tasks_and_quit(temp_db, default_team):
    from labflow.repl import run_script
    out = run_script(["tasks"])
    # Should contain the table header / dashes line.
    assert "title" in out
    assert "(no rows)" in out or "id" in out


def test_repl_team_show(temp_db, default_team):
    from labflow.repl import run_script
    out = run_script(["team"])
    assert default_team.slug in out


def test_repl_task_show_done(temp_db, default_team):
    from labflow import models
    from labflow.repl import run_script
    SessionLocal = temp_db.get_session_factory()
    with SessionLocal() as s:
        m = models.Meeting(team_id=default_team.id, title="t",
                           meeting_type="standup", transcript="", notes="")
        s.add(m)
        s.flush()
        t = models.Task(team_id=default_team.id, meeting_id=m.id,
                        title="repl test",
                        confidence=1.0, status="open", state="todo")
        s.add(t)
        s.commit()
        tid = t.id

    out = run_script([f"task {tid} show", f"task {tid} done",
                      f"task {tid} show"])
    assert "repl test" in out
    assert "marked done" in out
    assert "status=done" in out


def test_repl_help_lists_commands(temp_db, default_team):
    from labflow.repl import run_script
    out = run_script(["help"])
    assert "tasks" in out and "decisions" in out


# ============================================================ version bump
def test_version_is_013(temp_db, app_client):
    """Original v0.13 version assertion — kept loose so v0.14+ doesn't
    regress us back; the API version always advances forward."""
    spec = app_client.get("/openapi.json").json()
    # Major.minor must be at least 0.13 (lexicographic on the tuple).
    parts = tuple(int(x) for x in spec["info"]["version"].split(".")[:2])
    assert parts >= (0, 13)
