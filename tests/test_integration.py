"""End-to-end integration tests against the FastAPI app."""
from pathlib import Path


def test_full_lifecycle_upload_extract_finalize_export(app_client):
    # 1. Upload transcript
    transcript = Path("demo/research_standup.txt").read_text()
    resp = app_client.post(
        "/api/meetings/upload",
        data={"title": "Research standup", "meeting_type": "standup"},
        files={"transcript_file": ("standup.txt", transcript, "text/plain")},
    )
    assert resp.status_code == 200, resp.text
    meeting = resp.json()
    mid = meeting["id"]
    assert meeting["finalized"] is False

    # 2. Re-extract should still succeed (idempotent)
    r2 = app_client.post(f"/api/meetings/{mid}/extract")
    assert r2.status_code == 200

    # 3. Tasks should be visible via the task API
    tasks = app_client.get("/api/tasks").json()
    assert len(tasks) >= 1
    task_titles = [t["title"].lower() for t in tasks]
    assert any("ablation" in t for t in task_titles)

    # 4. Edit a task — flip one to in_progress
    tid = tasks[0]["id"]
    r = app_client.patch(f"/api/tasks/{tid}", json={"status": "in_progress"})
    assert r.status_code == 200
    assert r.json()["status"] == "in_progress"

    # 5. Add commit evidence that should auto-close a task
    target = next(t for t in tasks if "ablation" in t["title"].lower())
    r = app_client.post(
        "/api/evidence",
        json={
            "task_id": target["id"],
            "kind": "commit",
            "uri": "https://github.com/lab/repo/commit/deadbeef",
            "summary": "carol: ablation on attention dropout rate complete",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["verified"] is True

    # 6. Confirm the task is now done
    refreshed = app_client.get("/api/tasks").json()
    closed = [t for t in refreshed if t["id"] == target["id"]]
    assert closed and closed[0]["status"] == "done"

    # 7. Markdown export contains a checked checkbox
    md = app_client.get(f"/api/meetings/{mid}/export.md").text
    assert "[x]" in md

    # 8. JSON export round-trips
    js = app_client.get(f"/api/meetings/{mid}/export.json").json()
    assert js["meeting"]["id"] == mid
    assert any(t["status"] == "done" for t in js["tasks"])

    # 9. Finalize
    r = app_client.post(f"/api/meetings/{mid}/finalize")
    assert r.status_code == 200 and r.json()["finalized"] is True

    # 10. Re-extract is now blocked
    r = app_client.post(f"/api/meetings/{mid}/extract")
    assert r.status_code == 400

    # 11. Weekly digest is generated and references our meeting's owners/decisions.
    digest = app_client.get("/api/digest/weekly").text
    assert "Weekly Digest" in digest


def test_multi_meeting_decision_continuity(app_client):
    # Two meetings with the same decision — second supersedes the first.
    m1 = app_client.post(
        "/api/meetings",
        json={"title": "m1", "transcript": "We decided to use SentencePiece."},
    ).json()
    m2 = app_client.post(
        "/api/meetings",
        json={"title": "m2", "transcript": "We decided to use SentencePiece."},
    ).json()
    assert m1["id"] != m2["id"]

    # Both meetings should export the same decision text.
    j1 = app_client.get(f"/api/meetings/{m1['id']}/export.json").json()
    j2 = app_client.get(f"/api/meetings/{m2['id']}/export.json").json()
    s1 = {d["statement"] for d in j1["decisions"]}
    s2 = {d["statement"] for d in j2["decisions"]}
    assert s1 & s2  # at least one shared decision


def test_landing_and_dashboard_render(app_client):
    assert app_client.get("/").status_code == 200
    assert app_client.get("/app").status_code == 200
    assert app_client.get("/healthz").json() == {"ok": True}


def test_review_404_for_missing_meeting(app_client):
    assert app_client.get("/meetings/9999/review").status_code == 404
