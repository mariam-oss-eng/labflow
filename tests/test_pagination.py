"""Tests for pagination + filtering on list endpoints."""
from __future__ import annotations


def _seed_meetings(client, n=5):
    for i in range(n):
        client.post(
            "/api/meetings",
            json={
                "title": f"m{i}",
                "transcript": f"@alice will do task{i} tomorrow.",
                "meeting_type": "standup" if i % 2 == 0 else "review",
            },
        )


def test_meetings_pagination_envelope(app_client):
    _seed_meetings(app_client, 5)
    body = app_client.get("/api/meetings").json()
    assert "items" in body and "page" in body
    assert body["page"]["total"] == 5
    assert len(body["items"]) == 5


def test_meetings_pagination_limit(app_client):
    _seed_meetings(app_client, 5)
    body = app_client.get("/api/meetings?limit=2").json()
    assert len(body["items"]) == 2
    assert body["page"]["total"] == 5
    assert body["page"]["limit"] == 2
    assert body["page"]["offset"] == 0


def test_meetings_pagination_offset(app_client):
    _seed_meetings(app_client, 5)
    page1 = app_client.get("/api/meetings?limit=2&offset=0").json()["items"]
    page2 = app_client.get("/api/meetings?limit=2&offset=2").json()["items"]
    assert {m["id"] for m in page1} & {m["id"] for m in page2} == set()


def test_meetings_filter_by_type(app_client):
    _seed_meetings(app_client, 5)
    body = app_client.get("/api/meetings?meeting_type=review").json()
    assert all(m["meeting_type"] == "review" for m in body["items"])


def test_tasks_filter_by_status(app_client):
    _seed_meetings(app_client, 3)
    body = app_client.get("/api/tasks?status=open").json()
    assert all(t["status"] == "open" for t in body["items"])


def test_tasks_filter_by_owner(app_client):
    app_client.post("/api/meetings", json={
        "title": "m", "transcript": "@alice will retrain model. @bob will write doc."
    })
    body = app_client.get("/api/tasks?owner=alice").json()
    assert body["page"]["total"] >= 1


def test_max_page_size_clamped(app_client, monkeypatch):
    _seed_meetings(app_client, 3)
    # Asking for 99999 should be clamped to settings.max_page_size (default 200).
    body = app_client.get("/api/meetings?limit=99999").json()
    assert body["page"]["limit"] <= 200
