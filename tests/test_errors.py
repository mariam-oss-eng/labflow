"""Tests for the typed error envelope + global handlers."""


def test_404_returns_error_envelope(app_client):
    r = app_client.get("/api/meetings/9999/export.json")
    assert r.status_code == 404
    body = r.json()
    assert "error" in body
    assert body["error"]["code"] == "not_found"
    assert "request_id" in body["error"]


def test_validation_error_returns_envelope(app_client):
    # title is required
    r = app_client.post("/api/meetings", json={"meeting_type": "standup"})
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "invalid_request"
    assert isinstance(body["error"]["details"], list)


def test_payload_too_large(app_client, monkeypatch):
    huge = "x" * 1_500_000
    r = app_client.post("/api/meetings", json={"title": "big", "transcript": huge})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large"


def test_response_includes_request_id_header(app_client):
    r = app_client.get("/healthz")
    assert "x-request-id" in {k.lower() for k in r.headers.keys()}


def test_patch_task_extra_field_rejected(app_client):
    # Create a meeting with a task first
    m = app_client.post(
        "/api/meetings",
        json={"title": "m", "transcript": "@alice will retrain the model tomorrow."},
    ).json()
    assert m["id"]
    tasks = app_client.get("/api/tasks").json()["items"]
    tid = tasks[0]["id"]
    r = app_client.patch(f"/api/tasks/{tid}", json={"hacker": "yes"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"


def test_finalized_meeting_reextract_returns_409(app_client):
    m = app_client.post("/api/meetings", json={"title": "m", "transcript": "We decided X."}).json()
    app_client.post(f"/api/meetings/{m['id']}/finalize")
    r = app_client.post(f"/api/meetings/{m['id']}/extract")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"
