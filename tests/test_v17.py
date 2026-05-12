"""Tests for v0.17 — webhook DLQ, activity heatmap, API key rotation, TUI."""
from __future__ import annotations

import io
from datetime import datetime, timedelta


def _seed_audit(session, team_id, *, n=5, days_back=0):
    from labflow import audit, models
    for _ in range(n):
        evt = audit.record(
            session, team_id=team_id, action="test.event",
            entity_type="task", entity_id=None, actor="alice",
        )
        evt.created_at = datetime.utcnow() - timedelta(days=days_back)
    session.flush()


# ============================================================ webhook DLQ
def test_webhook_delivery_dead_letters_after_max_attempts(
    temp_db, session, default_team,
):
    from labflow import models, webhooks
    from labflow.webhooks import _MAX_ATTEMPTS

    sub = models.WebhookSubscription(
        team_id=default_team.id, url="https://example/h", event="*",
        secret="s", active=True,
    )
    session.add(sub)
    session.flush()

    webhooks.emit(session, team_id=default_team.id, event="x", payload={"a": 1})
    session.flush()

    def boom(url, body, headers):
        return 500, "kaboom"

    for _ in range(_MAX_ATTEMPTS):
        webhooks.deliver_pending(session, http_post=boom)

    d = session.execute(
        __import__("sqlalchemy").select(models.WebhookDelivery)
    ).scalar_one()
    assert d.attempts == _MAX_ATTEMPTS
    assert d.success is False
    assert d.dead_lettered_at is not None


def test_webhook_dlq_replay_resets_attempts(
    temp_db, session, default_team,
):
    from labflow import models, webhook_dlq, webhooks
    from labflow.webhooks import _MAX_ATTEMPTS

    sub = models.WebhookSubscription(
        team_id=default_team.id, url="https://example/h", event="*",
        secret="s", active=True,
    )
    session.add(sub)
    session.flush()
    webhooks.emit(session, team_id=default_team.id, event="x", payload={})
    session.flush()
    for _ in range(_MAX_ATTEMPTS):
        webhooks.deliver_pending(session, http_post=lambda *a: (500, ""))

    rows = webhook_dlq.list_dead(session, team_id=default_team.id)
    assert len(rows) == 1
    d = webhook_dlq.replay(session, team_id=default_team.id,
                           delivery_id=rows[0].id)
    assert d.attempts == 0
    assert d.dead_lettered_at is None


def test_webhook_dlq_discard_records_audit(
    temp_db, session, default_team,
):
    from labflow import audit, models, webhook_dlq, webhooks
    from labflow.webhooks import _MAX_ATTEMPTS

    sub = models.WebhookSubscription(
        team_id=default_team.id, url="https://example/h", event="*",
        secret="s", active=True,
    )
    session.add(sub)
    session.flush()
    webhooks.emit(session, team_id=default_team.id, event="x", payload={})
    session.flush()
    for _ in range(_MAX_ATTEMPTS):
        webhooks.deliver_pending(session, http_post=lambda *a: (500, ""))

    rows = webhook_dlq.list_dead(session, team_id=default_team.id)
    webhook_dlq.discard(session, team_id=default_team.id,
                        delivery_id=rows[0].id, actor="alice")
    # The delivery stays dead-lettered; audit gains a discard entry.
    events = session.execute(
        __import__("sqlalchemy").select(models.AuditEvent).where(
            models.AuditEvent.action == "webhook_dlq.discard",
        )
    ).scalars().all()
    assert events and events[0].actor == "alice"


def test_webhook_dlq_replay_rejects_non_dead(
    temp_db, app_client, session, default_team,
):
    from labflow import models, webhooks
    sub = models.WebhookSubscription(
        team_id=default_team.id, url="https://example/h", event="*",
        secret="s", active=True,
    )
    session.add(sub)
    session.flush()
    webhooks.emit(session, team_id=default_team.id, event="x", payload={})
    session.commit()

    d = session.execute(
        __import__("sqlalchemy").select(models.WebhookDelivery)
    ).scalar_one()
    r = app_client.post(f"/api/webhook-dlq/{d.id}/replay")
    assert r.status_code == 400


def test_webhook_dlq_stats_endpoint(temp_db, app_client):
    r = app_client.get("/api/webhook-dlq/stats")
    assert r.status_code == 200
    body = r.json()
    assert {"dead", "pending", "success"} == set(body)


# ============================================================ heatmap
def test_heatmap_counts_includes_zero_days(
    temp_db, session, default_team,
):
    from labflow import heatmap
    _seed_audit(session, default_team.id, n=3, days_back=0)
    counts = heatmap.daily_counts(session, team_id=default_team.id, days=14)
    assert len(counts) == 14
    today = datetime.utcnow().date().isoformat()
    assert counts[today] >= 3


def test_heatmap_svg_endpoint(temp_db, app_client, session, default_team):
    _seed_audit(session, default_team.id, n=2)
    session.commit()
    r = app_client.get("/api/heatmap.svg", params={"days": 30})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in r.content
    assert b"events" in r.content


def test_heatmap_rejects_excessive_days(temp_db, app_client):
    r = app_client.get("/api/heatmap", params={"days": 5000})
    assert r.status_code == 400


# ============================================================ key rotation
def test_key_rotation_creates_successor_with_grace(
    temp_db, session, default_team,
):
    from labflow import auth, key_rotation, models
    plaintext = auth.generate_api_key()
    old = models.ApiKey(
        team_id=default_team.id, name="orig",
        key_hash=auth.hash_api_key(plaintext),
    )
    session.add(old)
    session.flush()
    result = key_rotation.rotate(
        session, team_id=default_team.id, old_key_id=old.id, grace_hours=2,
    )
    assert result.new_token.startswith("lfk_")
    assert result.old_id == old.id
    session.refresh(old)
    assert old.rotation_grace_until is not None
    assert old.revoked_at is None  # still valid during the grace window
    new = session.get(models.ApiKey, result.new_id)
    assert new.rotated_from_id == old.id


def test_key_rotation_double_rotate_409(
    temp_db, session, default_team,
):
    from labflow import auth, key_rotation, models
    from labflow.errors import ConflictError
    import pytest

    old = models.ApiKey(
        team_id=default_team.id, name="orig",
        key_hash=auth.hash_api_key(auth.generate_api_key()),
    )
    session.add(old)
    session.flush()
    key_rotation.rotate(session, team_id=default_team.id, old_key_id=old.id)
    with pytest.raises(ConflictError):
        key_rotation.rotate(session, team_id=default_team.id, old_key_id=old.id)


def test_key_rotation_sweep_revokes_after_grace(
    temp_db, session, default_team,
):
    from labflow import auth, key_rotation, models
    old = models.ApiKey(
        team_id=default_team.id, name="orig",
        key_hash=auth.hash_api_key(auth.generate_api_key()),
    )
    session.add(old)
    session.flush()
    key_rotation.rotate(session, team_id=default_team.id, old_key_id=old.id,
                        grace_hours=1)
    # Force the grace window into the past.
    session.refresh(old)
    old.rotation_grace_until = datetime.utcnow() - timedelta(minutes=1)
    session.flush()
    n = key_rotation.sweep_expired(session, team_id=default_team.id)
    assert n == 1
    session.refresh(old)
    assert old.revoked_at is not None


def test_key_rotation_cancel_clears_grace(
    temp_db, session, default_team,
):
    from labflow import auth, key_rotation, models
    old = models.ApiKey(
        team_id=default_team.id, name="orig",
        key_hash=auth.hash_api_key(auth.generate_api_key()),
    )
    session.add(old)
    session.flush()
    key_rotation.rotate(session, team_id=default_team.id, old_key_id=old.id)
    key_rotation.cancel_rotation(session, team_id=default_team.id,
                                 old_key_id=old.id)
    session.refresh(old)
    assert old.rotation_grace_until is None


# ============================================================ TUI
def test_tui_render_snapshot_includes_team_and_stats(
    temp_db, session, default_team,
):
    from labflow import tui
    out = tui.render_snapshot(session, team=default_team)
    assert "LabFlow" in out
    assert default_team.slug in out
    assert "Tasks" in out
    assert "Webhook DLQ" in out
    assert "Activity" in out


def test_tui_run_once_writes_to_stream(
    temp_db, session, default_team,
):
    from labflow import tui
    buf = io.StringIO()
    tui.run(session, team=default_team, once=True, out=buf)
    text = buf.getvalue()
    assert "LabFlow" in text
