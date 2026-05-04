"""Tests for v0.10 — audit hash chain, signed backup/restore,
automation rules, forecasting, dashboards.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient


# --------------------------------------------------------------- helpers
def _seed_meeting(client: TestClient,
                  transcript: str = "We decided to switch to Postgres for v2. "
                                    "TODO: provision the staging cluster by Friday. "
                                    "Owner: @alice") -> tuple[int, int, int]:
    r = client.post("/api/meetings", json={
        "title": "v0.10 seed", "transcript": transcript, "notes": "",
    })
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    from labflow.db import get_session_factory
    from labflow import models
    with get_session_factory()() as s:
        d = s.query(models.Decision).filter_by(meeting_id=mid).first()
        t = s.query(models.Task).filter_by(meeting_id=mid).first()
        return mid, d.id, t.id


def _seed_audited(client: TestClient) -> int:
    """Create a task and transition it so we get a few chained audit rows."""
    _, _, tid = _seed_meeting(client)
    r = client.post(f"/api/tasks/{tid}/transition",
                    json={"to_state": "in_progress"})
    assert r.status_code == 200, r.text
    return tid


# =============================================================== audit chain
def test_audit_chain_starts_consistent(temp_db, app_client):
    _seed_audited(app_client)
    r = app_client.get("/api/audit/verify")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["checked"] >= 1
    assert body["first_break_id"] is None


def test_audit_chain_detects_tamper(temp_db, app_client):
    _seed_audited(app_client)
    # Tamper: rewrite an audit row's metadata in-place. Pick the most
    # recent chained row so it's definitely part of the verifiable chain.
    from labflow.db import get_session_factory
    from labflow import models
    with get_session_factory()() as s:
        target = s.query(models.AuditEvent).filter(
            models.AuditEvent.entry_hash.is_not(None)
        ).order_by(models.AuditEvent.id.desc()).first()
        assert target is not None
        target.metadata_json = '{"tampered":true}'
        s.commit()
    r = app_client.get("/api/audit/verify")
    body = r.json()
    assert body["ok"] is False
    assert body["first_break_id"] == target.id


def test_audit_chain_propagates_after_legacy_rows(temp_db, app_client):
    """Pre-v0.10 NULL-hash rows are skipped, then chain resumes."""
    from labflow.db import get_session_factory
    from labflow import models, audit as audit_mod
    # First write a legacy-style row directly (no chain fields).
    with get_session_factory()() as s:
        team = s.query(models.Team).first()
        s.add(models.AuditEvent(team_id=team.id, actor="legacy",
                                action="legacy.action", entity_type="x"))
        s.commit()
    # Then write a chained row via audit.record.
    with get_session_factory()() as s:
        team = s.query(models.Team).first()
        audit_mod.record(s, team_id=team.id, action="x.created",
                        entity_type="x", entity_id=1)
        s.commit()
    r = app_client.get("/api/audit/verify")
    body = r.json()
    assert body["ok"] is True
    assert body["skipped_legacy"] >= 1
    assert body["checked"] >= 1


# =============================================================== backup / restore
def test_backup_round_trip(temp_db, app_client):
    _seed_meeting(app_client)
    r = app_client.post("/api/admin/backup?secret=test-secret-123")
    assert r.status_code == 200, r.text
    envelope = r.json()
    assert envelope["signature"]
    assert envelope["header"]["row_count"] >= 1

    # preview should round-trip cleanly.
    r2 = app_client.post("/api/admin/restore/preview", json={
        "envelope": envelope, "secret": "test-secret-123",
    })
    assert r2.status_code == 200
    assert r2.json()["total_rows"] == envelope["header"]["row_count"]


def test_backup_signature_required(temp_db, app_client):
    _seed_meeting(app_client)
    r = app_client.post("/api/admin/backup?secret=secret-A")
    envelope = r.json()
    # Wrong secret on preview must fail validation.
    r2 = app_client.post("/api/admin/restore/preview", json={
        "envelope": envelope, "secret": "wrong-secret",
    })
    assert r2.status_code in (400, 422), r2.text


def test_backup_restore_into_new_team(temp_db, app_client):
    mid, did, tid = _seed_meeting(app_client)
    r = app_client.post("/api/admin/backup?secret=s3cret")
    envelope = r.json()
    r2 = app_client.post("/api/admin/restore/apply", json={
        "envelope": envelope, "secret": "s3cret", "new_slug": "restored",
    })
    assert r2.status_code == 201, r2.text
    body = r2.json()
    assert body["team_slug"] == "restored"
    assert body["inserted"]["meetings"] >= 1
    assert body["inserted"]["tasks"] >= 1


def test_backup_restore_rejects_existing_slug(temp_db, app_client):
    _seed_meeting(app_client)
    r = app_client.post("/api/admin/backup?secret=ss")
    env = r.json()
    # "default" already exists from bootstrap.
    r2 = app_client.post("/api/admin/restore/apply", json={
        "envelope": env, "secret": "ss", "new_slug": "default",
    })
    assert r2.status_code in (409, 422), r2.text


# =============================================================== automation
def test_automation_rule_create_and_list(temp_db, app_client):
    r = app_client.post("/api/automation/rules", json={
        "name": "review-tag",
        "trigger_event": "task.transition",
        "condition": {"to_state": "in_review"},
        "actions": [
            {"kind": "tag", "params": {"tag": "needs-review"}},
        ],
    })
    assert r.status_code == 201, r.text
    rules = app_client.get("/api/automation/rules").json()["rules"]
    assert any(r["name"] == "review-tag" for r in rules)


def test_automation_rule_validation_rejects_bad_action(temp_db, app_client):
    r = app_client.post("/api/automation/rules", json={
        "name": "bad", "trigger_event": "task.transition",
        "actions": [{"kind": "delete_database"}],
    })
    assert r.status_code in (400, 422), r.text


def test_automation_rule_fires_on_event(temp_db, app_client):
    """A tag rule should fire and create an automation.tag audit row."""
    _, _, tid = _seed_meeting(app_client)
    r = app_client.post("/api/automation/rules", json={
        "name": "auto-tag-progress",
        "trigger_event": "task.transition",
        "condition": {"to": "in_progress"},
        "actions": [{"kind": "tag", "params": {"tag": "wip"}}],
    })
    assert r.status_code == 201
    # Trigger transition.
    rt = app_client.post(f"/api/tasks/{tid}/transition",
                         json={"to_state": "in_progress"})
    assert rt.status_code == 200, rt.text
    # The rule's fires counter should advance.
    rules = app_client.get("/api/automation/rules").json()["rules"]
    rule = next(r for r in rules if r["name"] == "auto-tag-progress")
    assert rule["fires"] >= 1
    # An automation.tag audit row should exist for that task.
    from labflow.db import get_session_factory
    from labflow import models
    with get_session_factory()() as s:
        rows = s.query(models.AuditEvent).filter_by(
            action="automation.tag", entity_type="task", entity_id=tid,
        ).all()
        assert len(rows) == 1


def test_automation_rule_delete(temp_db, app_client):
    app_client.post("/api/automation/rules", json={
        "name": "dropme", "trigger_event": "task.transition",
        "actions": [{"kind": "notify", "params": {"message": "x"}}],
    })
    r = app_client.delete("/api/automation/rules/dropme")
    assert r.status_code == 200
    rules = app_client.get("/api/automation/rules").json()["rules"]
    assert not any(r["name"] == "dropme" for r in rules)


# =============================================================== forecasting
def test_forecast_sprint_handles_missing(temp_db, app_client):
    r = app_client.get("/api/forecast/sprint/no-such-sprint")
    assert r.status_code in (400, 404), r.text


def test_forecast_sprint_with_data(temp_db, app_client):
    starts = (datetime.utcnow() - timedelta(days=2)).isoformat()
    ends = (datetime.utcnow() + timedelta(days=5)).isoformat()
    rs = app_client.post("/api/sprints", json={
        "name": "F-1", "starts_at": starts, "ends_at": ends, "slug": "f-1",
    })
    assert rs.status_code == 201, rs.text
    _, _, tid = _seed_meeting(app_client)
    app_client.post("/api/sprints/f-1/assign", json={"task_id": tid})
    r = app_client.get("/api/forecast/sprint/f-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["method"] == "linear_regression"
    assert "slope_per_day" in body
    assert "eta_iso" in body


def test_forecast_task_no_history(temp_db, app_client):
    _, _, tid = _seed_meeting(app_client)
    r = app_client.get(f"/api/forecast/task/{tid}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_id"] == tid
    # First task has no closed history → no_history.
    assert body["method"] == "no_history"


def test_forecast_task_uses_history(temp_db, app_client):
    """After a closed task exists, ETA is computed."""
    from labflow.db import get_session_factory
    from labflow import models
    from datetime import datetime, timedelta
    _, _, tid_open = _seed_meeting(app_client)
    # Create a synthetic closed task with a 4-hour cycle time so the
    # team-fallback path can pick it up.
    with get_session_factory()() as s:
        team = s.query(models.Team).first()
        m = s.query(models.Meeting).first()
        for i in range(3):
            t = models.Task(
                team_id=team.id, meeting_id=m.id,
                title=f"closed task {i}", status="closed",
                created_at=datetime.utcnow() - timedelta(hours=4),
                closed_at=datetime.utcnow(),
            )
            s.add(t)
        s.commit()
    r = app_client.get(f"/api/forecast/task/{tid_open}")
    body = r.json()
    assert body["method"] in ("median_cycle_time", "team_fallback")
    assert body["median_hours"] is not None
    assert body["eta_iso"] is not None


# =============================================================== dashboards
def test_dashboards_widget_catalogue(temp_db, app_client):
    r = app_client.get("/api/dashboards/widgets")
    assert r.status_code == 200
    kinds = {w["kind"] for w in r.json()["widgets"]}
    assert {"open_tasks", "recent_decisions", "sla_breaches",
            "sprint_burndown", "automation_status"}.issubset(kinds)


def test_dashboards_upsert_and_render(temp_db, app_client):
    _seed_meeting(app_client)
    r = app_client.post("/api/dashboards", json={
        "slug": "home", "name": "Home",
        "layout": [
            {"kind": "open_tasks", "params": {"limit": 5}},
            {"kind": "recent_decisions", "params": {"limit": 3}},
        ],
    })
    assert r.status_code == 201, r.text
    r2 = app_client.get("/api/dashboards/home/data")
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["slug"] == "home"
    assert len(body["widgets"]) == 2
    # First widget should have items list.
    assert "items" in body["widgets"][0]["data"]


def test_dashboards_rejects_unknown_widget_kind(temp_db, app_client):
    r = app_client.post("/api/dashboards", json={
        "slug": "bad", "name": "Bad",
        "layout": [{"kind": "nuclear_launch"}],
    })
    assert r.status_code in (400, 422), r.text
