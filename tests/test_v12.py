"""Tests for v0.12 — kanban board, recurring tasks, API key quotas,
bulk task ops, per-key digest scheduling.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest


# ============================================================ kanban board
def test_board_default_workflow_returns_columns(temp_db, app_client):
    # Create a meeting with a few tasks so the board has content.
    r = app_client.post("/api/meetings", json={
        "title": "kickoff",
        "transcript": (
            "We agreed to ship v1. "
            "@alice will write the spec by Friday. "
            "@bob will design the schema by Monday."
        ),
        "notes": "",
    })
    assert r.status_code == 201
    # The board endpoint returns the team's default workflow.
    r = app_client.get("/api/board/_default")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "workflow" in body
    assert isinstance(body["columns"], list) and body["columns"]
    state_keys = {c["state"] for c in body["columns"]}
    # Default workflow ships with open / in_progress / in_review / blocked / closed.
    assert {"open", "closed"}.issubset(state_keys)
    assert body["total"] >= 2


def test_board_html_renders(temp_db, app_client):
    r = app_client.post("/api/meetings", json={
        "title": "k",
        "transcript": "@alice will write the spec by Friday.",
        "notes": "",
    })
    assert r.status_code == 201
    r = app_client.get("/app/board/_default")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "📋" in r.text  # title bar emoji
    assert "write the spec" in r.text


def test_board_unknown_workflow_404(temp_db, app_client):
    r = app_client.get("/api/board/does-not-exist")
    assert r.status_code == 404


# ========================================================= recurring tasks
def test_recurring_create_then_list(temp_db, app_client):
    r = app_client.post("/api/recurring-tasks", json={
        "slug": "weekly-standup-followup",
        "cadence": "weekly", "day_of_week": 0,
        "template_title": "Weekly standup follow-up",
        "template_priority": "medium",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["slug"] == "weekly-standup-followup"
    assert "next_run_at" in body

    r = app_client.get("/api/recurring-tasks")
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(i["slug"] == "weekly-standup-followup" for i in items)


def test_recurring_invalid_cadence_rejected(temp_db, app_client):
    r = app_client.post("/api/recurring-tasks", json={
        "slug": "bad",
        "cadence": "hourly",  # not supported
        "template_title": "x",
    })
    assert r.status_code == 400


def test_recurring_run_materialises_due(temp_db, default_team):
    from labflow import models, recurring
    SessionLocal = temp_db.get_session_factory()
    with SessionLocal() as s:
        # Create a daily template due in the past so it fires immediately.
        rt = models.RecurringTask(
            team_id=default_team.id,
            slug="daily-cleanup",
            cadence="daily", interval=1,
            template_title="Cleanup tmp dir",
            next_run_at=datetime.utcnow() - timedelta(days=1),
            active=True,
        )
        s.add(rt)
        s.commit()

        created = recurring.materialize_due(s, team_id=default_team.id)
        s.commit()
        assert len(created) == 1
        assert created[0].title == "Cleanup tmp dir"
        # Re-running is a no-op because next_run_at was advanced.
        again = recurring.materialize_due(s, team_id=default_team.id)
        assert again == []


def test_recurring_next_run_after_monthly_clamps_dom():
    from labflow.recurring import next_run_after
    # Day 31 in February → clamped to 28 (or 29 in leap years).
    nxt = next_run_after(
        after=datetime(2026, 1, 31, 12, 0),
        cadence="monthly", day_of_month=31,
    )
    assert nxt.day in (28, 29)
    assert nxt.month == 2


def test_recurring_delete(temp_db, app_client):
    app_client.post("/api/recurring-tasks", json={
        "slug": "to-delete", "cadence": "daily",
        "template_title": "transient",
    })
    r = app_client.delete("/api/recurring-tasks/to-delete")
    assert r.status_code == 200
    r = app_client.delete("/api/recurring-tasks/to-delete")
    # Idempotent: 404 second time.
    assert r.status_code == 404


# ============================================================ API quotas
def test_quota_blocks_when_exceeded(temp_db, default_team):
    from labflow import models, quotas
    SessionLocal = temp_db.get_session_factory()
    with SessionLocal() as s:
        key = models.ApiKey(team_id=default_team.id, name="t",
                            key_hash="x" * 64)
        s.add(key)
        s.commit()
        kid = key.id
        quotas.set_quota(s, team_id=default_team.id, api_key_id=kid,
                         daily_limit=2)
        s.commit()

        for _ in range(2):
            n, lim = quotas.increment_and_check(
                s, team_id=default_team.id, api_key_id=kid,
            )
            s.commit()
        assert n == 2 and lim == 2

        with pytest.raises(quotas.QuotaExceededError):
            quotas.increment_and_check(
                s, team_id=default_team.id, api_key_id=kid,
            )
        s.commit()


def test_quota_no_quota_means_unlimited(temp_db, default_team):
    from labflow import models, quotas
    SessionLocal = temp_db.get_session_factory()
    with SessionLocal() as s:
        key = models.ApiKey(team_id=default_team.id, name="t",
                            key_hash="y" * 64)
        s.add(key)
        s.commit()
        for _ in range(50):
            n, lim = quotas.increment_and_check(
                s, team_id=default_team.id, api_key_id=key.id,
            )
        assert lim is None and n == 50
        s.commit()


def test_quota_usage_summary(temp_db, default_team):
    from labflow import models, quotas
    SessionLocal = temp_db.get_session_factory()
    with SessionLocal() as s:
        k1 = models.ApiKey(team_id=default_team.id, name="a",
                           key_hash="a" * 64)
        k2 = models.ApiKey(team_id=default_team.id, name="b",
                           key_hash="b" * 64)
        s.add_all([k1, k2])
        s.commit()
        for _ in range(3):
            quotas.increment_and_check(s, team_id=default_team.id,
                                       api_key_id=k1.id)
        quotas.increment_and_check(s, team_id=default_team.id,
                                   api_key_id=k2.id)
        s.commit()
        usage = quotas.usage_summary(s, team_id=default_team.id)
        # k1 should sort before k2 (count desc).
        assert usage[0]["api_key_id"] == k1.id and usage[0]["count"] == 3
        assert usage[1]["api_key_id"] == k2.id and usage[1]["count"] == 1


# ====================================================== bulk task operations
def test_bulk_assign_and_priority(temp_db, app_client):
    # Seed: three tasks via a meeting transcript.
    r = app_client.post("/api/meetings", json={
        "title": "bulk seed",
        "transcript": ("@alice will ship A by tomorrow. "
                       "@bob will ship B by tomorrow. "
                       "@cathy will ship C by tomorrow."),
        "notes": "",
    })
    assert r.status_code == 201
    r = app_client.get("/api/tasks")
    ids = [t["id"] for t in r.json()["items"]][:3]
    assert len(ids) == 3

    r = app_client.post("/api/tasks/bulk", json={
        "op": "set_priority",
        "task_ids": ids,
        "args": {"priority": "high"},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == 3 and body["failed"] == 0


def test_bulk_partial_failure_isolated(temp_db, app_client):
    r = app_client.post("/api/meetings", json={
        "title": "x",
        "transcript": "@alice will ship A by tomorrow.",
        "notes": "",
    })
    assert r.status_code == 201
    real_id = app_client.get("/api/tasks").json()["items"][0]["id"]
    r = app_client.post("/api/tasks/bulk", json={
        "op": "set_priority",
        "task_ids": [real_id, 99999],
        "args": {"priority": "low"},
    })
    body = r.json()
    assert body["applied"] == 1 and body["failed"] == 1
    err = next(x for x in body["results"] if x["task_id"] == 99999)
    assert err["error"] == "not_found"


def test_bulk_unknown_op_rejected(temp_db, app_client):
    r = app_client.post("/api/tasks/bulk", json={
        "op": "explode", "task_ids": [1],
    })
    assert r.status_code == 400


# =================================================== digest hour scheduling
def test_notification_digest_hour_setget(temp_db, app_client):
    # Auth disabled in fixture, but middleware still mints / resolves a key.
    r = app_client.put("/api/notifications/me", json={
        "digest_cadence": "daily",
        "digest_hour_utc": 9,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["digest_hour_utc"] == 9
    assert body["digest_cadence"] == "daily"

    # Clear via -1
    r = app_client.put("/api/notifications/me", json={"digest_hour_utc": -1})
    assert r.status_code == 200
    assert r.json()["digest_hour_utc"] is None


def test_notification_digest_hour_validation(temp_db, app_client):
    r = app_client.put("/api/notifications/me", json={"digest_hour_utc": 24})
    assert r.status_code == 400


def test_notification_digest_due_lists_keys(temp_db, default_team, app_client):
    # First touch creates a notif pref row for the request key.
    r = app_client.put("/api/notifications/me", json={
        "digest_cadence": "daily", "digest_hour_utc": 7,
    })
    assert r.status_code == 200
    r = app_client.get("/api/notifications/digest-due?hour=7")
    assert r.status_code == 200
    body = r.json()
    assert body["hour_utc"] == 7
    assert isinstance(body["api_key_ids"], list) and body["api_key_ids"]
