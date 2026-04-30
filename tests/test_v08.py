"""Tests for v0.8 — workflows, sprints, DAG/critical-path, ACL, share links,
CSV export, Slack notifier, scopes."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient


# ============================================================ helpers
def _seed_meeting(client: TestClient, transcript: str = "We decided to switch to Postgres for v2. TODO: provision the staging cluster by Friday. Owner: @alice") -> tuple[int, int, int]:
    r = client.post("/api/meetings", json={
        "title": "v0.8 seed", "transcript": transcript, "notes": "",
    })
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    from labflow.db import get_session_factory
    from labflow import models
    with get_session_factory()() as s:
        d = s.query(models.Decision).filter_by(meeting_id=mid).first()
        t = s.query(models.Task).filter_by(meeting_id=mid).first()
        assert d is not None and t is not None
        return mid, d.id, t.id


# ============================================================ workflows
def test_default_workflow_auto_created(temp_db, app_client):
    r = app_client.get("/api/workflows")
    assert r.status_code == 200
    wfs = r.json()["workflows"]
    assert any(w["name"] == "default" and w["is_default"] for w in wfs)
    default = next(w for w in wfs if w["is_default"])
    assert "open" in default["definition"]["states"]
    assert default["definition"]["initial"] == "open"


def test_create_custom_workflow(temp_db, app_client):
    body = {
        "name": "research",
        "definition": {
            "states": ["draft", "running", "done"],
            "initial": "draft",
            "terminal": ["done"],
            "transitions": [
                {"from": "draft", "to": "running"},
                {"from": "running", "to": "done"},
            ],
        },
    }
    r = app_client.post("/api/workflows", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "research"


def test_workflow_definition_validated(temp_db, app_client):
    r = app_client.post("/api/workflows", json={
        "name": "bad", "definition": {"states": [], "initial": "x"},
    })
    assert r.status_code in (400, 422)


def test_task_transition_happy_path(temp_db, app_client):
    _mid, _did, tid = _seed_meeting(app_client)
    r = app_client.post(f"/api/tasks/{tid}/transition",
                        json={"to_state": "in_progress"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["from_state"] == "open"
    assert body["to_state"] == "in_progress"
    assert body["sla_breach_at"] is None
    # second hop sets SLA
    r = app_client.post(f"/api/tasks/{tid}/transition",
                        json={"to_state": "in_review"})
    assert r.status_code == 200
    assert r.json()["sla_breach_at"] is not None


def test_task_transition_illegal(temp_db, app_client):
    _mid, _did, tid = _seed_meeting(app_client)
    r = app_client.post(f"/api/tasks/{tid}/transition",
                        json={"to_state": "in_review"})  # no direct edge
    assert r.status_code in (400, 422)


def test_task_transition_to_terminal_closes(temp_db, app_client):
    _mid, _did, tid = _seed_meeting(app_client)
    # admin override edge: any -> closed
    r = app_client.post(f"/api/tasks/{tid}/transition",
                        json={"to_state": "closed"})
    assert r.status_code == 200, r.text
    assert r.json()["is_terminal"]
    # status should be "closed"
    from labflow.db import get_session_factory
    from labflow import models
    with get_session_factory()() as s:
        t = s.get(models.Task, tid)
        assert t.status == "closed"
        assert t.closed_at is not None


def test_sla_sweep(temp_db, app_client):
    _mid, _did, tid = _seed_meeting(app_client)
    # transition to in_progress (no SLA), then to in_review (SLA=48h)
    app_client.post(f"/api/tasks/{tid}/transition", json={"to_state": "in_progress"})
    app_client.post(f"/api/tasks/{tid}/transition", json={"to_state": "in_review"})
    # forcibly age the SLA into the past
    from labflow.db import get_session_factory
    from labflow import models
    with get_session_factory()() as s:
        t = s.get(models.Task, tid)
        t.sla_breach_at = datetime.utcnow() - timedelta(hours=1)
        s.commit()
    r = app_client.post("/api/admin/sla/sweep")
    assert r.status_code == 200
    assert r.json()["count"] == 1


# ============================================================ sprints
def test_sprint_lifecycle(temp_db, app_client):
    starts = datetime.utcnow().isoformat()
    ends = (datetime.utcnow() + timedelta(days=14)).isoformat()
    r = app_client.post("/api/sprints", json={
        "name": "Sprint 42", "starts_at": starts, "ends_at": ends,
        "goal": "Ship the new graph view",
    })
    assert r.status_code == 201, r.text
    slug = r.json()["slug"]
    assert slug == "sprint-42"
    # list returns it
    r = app_client.get("/api/sprints")
    assert any(s["slug"] == slug for s in r.json()["sprints"])
    # close
    r = app_client.post(f"/api/sprints/{slug}/close")
    assert r.status_code == 200
    assert r.json()["active"] is False


def test_sprint_invalid_window(temp_db, app_client):
    starts = datetime.utcnow().isoformat()
    ends = (datetime.utcnow() - timedelta(days=1)).isoformat()
    r = app_client.post("/api/sprints", json={
        "name": "Bad", "starts_at": starts, "ends_at": ends,
    })
    assert r.status_code in (400, 422)


def test_sprint_burndown(temp_db, app_client):
    _mid, _did, tid = _seed_meeting(app_client)
    starts = (datetime.utcnow() - timedelta(days=2)).isoformat()
    ends = (datetime.utcnow() + timedelta(days=5)).isoformat()
    r = app_client.post("/api/sprints", json={
        "name": "S1", "starts_at": starts, "ends_at": ends,
    })
    slug = r.json()["slug"]
    r = app_client.post(f"/api/sprints/{slug}/assign", json={"task_id": tid})
    assert r.status_code == 200
    r = app_client.get(f"/api/sprints/{slug}/burndown")
    assert r.status_code == 200
    body = r.json()
    assert body["sprint"]["task_count"] == 1
    assert len(body["days"]) >= 3


# ============================================================ DAG / critical path
def test_dependencies_and_critical_path(temp_db, app_client):
    _mid, _did, t1 = _seed_meeting(app_client)
    # add two more tasks
    _mid2, _, t2 = _seed_meeting(app_client, "We decided to provision the cluster. TODO: provision GPU cluster. Owner: @bob")
    _mid3, _, t3 = _seed_meeting(app_client, "We decided to publish. TODO: write the paper draft. Owner: @carol")
    r = app_client.post(f"/api/tasks/{t2}/depends_on", json={"depends_on_id": t1})
    assert r.status_code == 201
    r = app_client.post(f"/api/tasks/{t3}/depends_on", json={"depends_on_id": t2})
    assert r.status_code == 201
    # cycle attempt
    r = app_client.post(f"/api/tasks/{t1}/depends_on", json={"depends_on_id": t3})
    assert r.status_code == 409
    r = app_client.get("/api/tasks/critical-path")
    assert r.status_code == 200
    body = r.json()
    # critical path goes t1 -> t2 -> t3
    assert body["path"] == [t1, t2, t3]
    assert len(body["nodes"]) >= 3


def test_dag_self_dep_rejected(temp_db, app_client):
    _mid, _did, t1 = _seed_meeting(app_client)
    r = app_client.post(f"/api/tasks/{t1}/depends_on", json={"depends_on_id": t1})
    assert r.status_code in (400, 422)


# ============================================================ ACLs
def test_acl_grant_and_revoke(temp_db, app_client):
    _mid, _did, tid = _seed_meeting(app_client)
    r = app_client.post("/api/acl", json={
        "entity_type": "task", "entity_id": tid,
        "api_key_id": None, "permission": "write",
    })
    assert r.status_code == 201
    aid = r.json()["id"]
    r = app_client.get("/api/acl",
                       params={"entity_type": "task", "entity_id": tid})
    assert len(r.json()["acls"]) == 1
    r = app_client.delete(f"/api/acl/{aid}")
    assert r.status_code == 200
    r = app_client.get("/api/acl",
                       params={"entity_type": "task", "entity_id": tid})
    assert r.json()["acls"] == []


def test_acl_logic_isolated(temp_db, default_team):
    """Direct test of the allow logic without HTTP."""
    from labflow.db import get_session_factory
    from labflow import acl, models
    SF = get_session_factory()
    with SF() as s:
        m = models.Meeting(team_id=default_team.id, title="t",
                           transcript="", notes="")
        s.add(m); s.flush()
        # No ACLs => allowed.
        assert acl.is_allowed(s, team_id=default_team.id,
                              entity_type="meeting", entity_id=m.id,
                              api_key_id=99, permission="read")
        # Grant key=1 read.
        acl.grant(s, team_id=default_team.id, entity_type="meeting",
                  entity_id=m.id, api_key_id=1, permission="read")
        # key=1 OK, key=2 denied (because ACLs now exist).
        assert acl.is_allowed(s, team_id=default_team.id,
                              entity_type="meeting", entity_id=m.id,
                              api_key_id=1, permission="read")
        assert not acl.is_allowed(s, team_id=default_team.id,
                                  entity_type="meeting", entity_id=m.id,
                                  api_key_id=2, permission="read")
        # write needs >= write
        assert not acl.is_allowed(s, team_id=default_team.id,
                                  entity_type="meeting", entity_id=m.id,
                                  api_key_id=1, permission="write")


# ============================================================ share links
def test_share_link_resolve(temp_db, app_client):
    mid, _did, _tid = _seed_meeting(app_client)
    r = app_client.post("/api/share-links", json={
        "entity_type": "meeting", "entity_id": mid, "ttl_hours": 24,
    })
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    r = app_client.get(f"/api/share/{token}")
    assert r.status_code == 200
    assert r.json()["type"] == "meeting"


def test_share_link_passcode(temp_db, app_client):
    mid, _did, _tid = _seed_meeting(app_client)
    r = app_client.post("/api/share-links", json={
        "entity_type": "meeting", "entity_id": mid, "passcode": "swordfish",
    })
    token = r.json()["token"]
    # missing passcode rejected
    r = app_client.get(f"/api/share/{token}")
    assert r.status_code == 401
    # wrong passcode rejected
    r = app_client.get(f"/api/share/{token}", params={"passcode": "wrong"})
    assert r.status_code == 401
    # correct passcode accepted
    r = app_client.get(f"/api/share/{token}", params={"passcode": "swordfish"})
    assert r.status_code == 200


def test_share_link_expired(temp_db, app_client):
    mid, _did, _tid = _seed_meeting(app_client)
    r = app_client.post("/api/share-links", json={
        "entity_type": "meeting", "entity_id": mid, "ttl_hours": 1,
    })
    token = r.json()["token"]
    # forcibly expire
    from labflow.db import get_session_factory
    from labflow import models
    with get_session_factory()() as s:
        link = s.query(models.ShareLink).order_by(models.ShareLink.id.desc()).first()
        link.expires_at = datetime.utcnow() - timedelta(hours=1)
        s.commit()
    r = app_client.get(f"/api/share/{token}")
    assert r.status_code == 401


# ============================================================ CSV exports
def test_tasks_csv(temp_db, app_client):
    _seed_meeting(app_client)
    r = app_client.get("/api/exports/tasks.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    body = r.text
    assert body.startswith("\ufeff")  # BOM
    # header line present, with v0.8 columns
    assert "id,title,status,state,owner" in body
    # at least the seeded task in body
    assert "\r\n" in body  # RFC 4180 line terminator


def test_decisions_csv(temp_db, app_client):
    _seed_meeting(app_client)
    r = app_client.get("/api/exports/decisions.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    body = r.text
    assert "id,statement,rationale" in body


# ============================================================ Slack notifier (offline)
def test_slack_render_digest_offline():
    from labflow import slack as slack_mod
    weekly = {
        "counts": {"meetings": 3, "decisions": 5,
                   "tasks_opened": 8, "tasks_closed": 4},
        "top_open_tasks": [
            {"title": "Ship paper draft"},
            {"title": "Provision GPU cluster"},
        ],
    }
    msg = slack_mod.render_digest(weekly)
    assert "weekly digest" in msg.text
    assert any(b["type"] == "header" for b in msg.blocks)
    assert any("Ship paper draft" in json.dumps(b) for b in msg.blocks)


# ============================================================ scopes
def test_scopes_parse_and_check():
    from labflow import scopes as scopes_mod
    from labflow.models import ApiKey
    # All scopes when None
    assert scopes_mod.has_scope(None, "write")
    k = ApiKey(team_id=1, name="x", key_hash="h",
               scopes=scopes_mod.serialise_scopes(["read"]))
    assert scopes_mod.has_scope(k, "read")
    assert not scopes_mod.has_scope(k, "write")
    # admin scope grants all
    k2 = ApiKey(team_id=1, name="x", key_hash="h",
                scopes=scopes_mod.serialise_scopes(["admin"]))
    assert scopes_mod.has_scope(k2, "write")
    assert scopes_mod.has_scope(k2, "webhook:emit")
    # invalid scope rejected at parse time
    with pytest.raises(ValueError):
        scopes_mod.parse_scopes("read,nonsense")
