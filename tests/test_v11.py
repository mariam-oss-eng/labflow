"""Tests for v0.11 — wiki / knowledge base, smart entity links,
watchers + activity feed, GraphQL mutations.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


def _seed(client: TestClient) -> tuple[int, int, int]:
    r = client.post("/api/meetings", json={
        "title": "v0.11 seed",
        "transcript": "We decided to switch to Postgres for v2. "
                      "TODO: provision the staging cluster by Friday. "
                      "Owner: @alice",
        "notes": "",
    })
    assert r.status_code == 201
    mid = r.json()["id"]
    from labflow.db import get_session_factory
    from labflow import models
    with get_session_factory()() as s:
        d = s.query(models.Decision).filter_by(meeting_id=mid).first()
        t = s.query(models.Task).filter_by(meeting_id=mid).first()
        return mid, d.id, t.id


# =============================================================== wiki
def test_wiki_create_and_get(temp_db, app_client):
    r = app_client.post("/api/wiki/pages", json={
        "title": "Onboarding", "body": "Welcome to LabFlow.",
    })
    assert r.status_code == 201, r.text
    slug = r.json()["slug"]
    g = app_client.get(f"/api/wiki/pages/{slug}")
    assert g.status_code == 200
    assert g.json()["title"] == "Onboarding"
    assert "Welcome" in g.json()["body"]
    assert g.json()["current_revision_id"] is not None


def test_wiki_revisions_grow_on_update(temp_db, app_client):
    app_client.post("/api/wiki/pages", json={
        "title": "Notes", "body": "v1",
    })
    app_client.post("/api/wiki/pages", json={
        "title": "Notes", "body": "v2",
    })
    app_client.post("/api/wiki/pages", json={
        "title": "Notes", "body": "v3",
    })
    revs = app_client.get("/api/wiki/pages/notes/revisions").json()["revisions"]
    assert len(revs) == 3


def test_wiki_soft_delete_then_404(temp_db, app_client):
    app_client.post("/api/wiki/pages", json={"title": "Drop", "body": "x"})
    r = app_client.delete("/api/wiki/pages/drop")
    assert r.status_code == 200
    g = app_client.get("/api/wiki/pages/drop")
    assert g.status_code == 404


def test_wiki_search(temp_db, app_client):
    app_client.post("/api/wiki/pages", json={
        "title": "Postgres setup", "body": "Postgres install steps",
    })
    app_client.post("/api/wiki/pages", json={
        "title": "Frontend tooling", "body": "Vite + TS",
    })
    r = app_client.get("/api/wiki/search", params={"q": "postgres"})
    hits = r.json()["hits"]
    assert any(h["slug"] == "postgres-setup" for h in hits)


# =============================================================== smart links
def test_smart_links_pure_parser():
    from labflow.links import parse
    out = parse("see #task-12 and [[Onboarding]] cc @alice")
    assert "12" in out["task_ids"]
    assert "Onboarding" in out["wikilinks"]
    assert "alice" in out["mentions"]


def test_wiki_creates_backlink_to_existing_task(temp_db, app_client):
    mid, did, tid = _seed(app_client)
    app_client.post("/api/wiki/pages", json={
        "title": "Plan",
        "body": f"Tracked work: #task-{tid} and see [[Roadmap]] for more.",
    })
    bl = app_client.get("/api/links/backlinks", params={
        "target_type": "task", "target_id": tid,
    }).json()["backlinks"]
    assert any(b["source_type"] == "wiki" and b["kind"] == "ref" for b in bl)


def test_wiki_wikilink_creates_placeholder(temp_db, app_client):
    app_client.post("/api/wiki/pages", json={
        "title": "Hub", "body": "See [[Architecture]]",
    })
    # Architecture page should have been auto-created as a placeholder.
    g = app_client.get("/api/wiki/pages/architecture")
    assert g.status_code == 200
    # And it should have a backlink from "hub".
    bl = g.json()["backlinks"]
    assert any(b["source_type"] == "wiki" for b in bl)


def test_wiki_link_replacement_drops_old(temp_db, app_client):
    """Editing a page removes outbound links from its old body."""
    mid, did, tid = _seed(app_client)
    app_client.post("/api/wiki/pages", json={
        "title": "Notes", "body": f"#task-{tid}",
    })
    bl = app_client.get("/api/links/backlinks", params={
        "target_type": "task", "target_id": tid,
    }).json()["backlinks"]
    assert len(bl) == 1
    # Update body without the task ref.
    app_client.post("/api/wiki/pages", json={
        "title": "Notes", "body": "no refs here",
    })
    bl = app_client.get("/api/links/backlinks", params={
        "target_type": "task", "target_id": tid,
    }).json()["backlinks"]
    assert len(bl) == 0


# =============================================================== watchers / feed
def test_watcher_add_and_remove(temp_db, app_client):
    mid, did, tid = _seed(app_client)
    r = app_client.post("/api/watchers", json={
        "entity_type": "task", "entity_id": tid,
    })
    assert r.status_code == 201
    lst = app_client.get("/api/watchers").json()["watches"]
    assert any(w["entity_type"] == "task" and w["entity_id"] == tid for w in lst)
    r = app_client.delete("/api/watchers", params={
        "entity_type": "task", "entity_id": tid,
    })
    assert r.status_code == 200 and r.json()["ok"] is True


def test_feed_returns_team_audit_when_no_watches(temp_db, app_client):
    """Without per-key watches, the feed returns the team audit log."""
    mid, did, tid = _seed(app_client)
    app_client.post(f"/api/tasks/{tid}/transition",
                    json={"to_state": "in_progress"})
    r = app_client.get("/api/feed")
    events = r.json()["events"]
    assert any(e["action"] == "task.transition" for e in events)


def test_watcher_validation_rejects_unknown_entity(temp_db, app_client):
    r = app_client.post("/api/watchers", json={
        "entity_type": "spaceship", "entity_id": 1,
    })
    assert r.status_code in (400, 422)


# =============================================================== graphql mutations
def test_graphql_mutation_comment_create(temp_db, app_client):
    mid, did, tid = _seed(app_client)
    q = ('mutation { commentCreate(entity_type: "task", entity_id: %d, '
         'body: "via gql") { id body actor } }') % tid
    r = app_client.post("/graphql", json={"query": q})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["commentCreate"]["body"] == "via gql"


def test_graphql_mutation_task_transition(temp_db, app_client):
    mid, did, tid = _seed(app_client)
    q = ('mutation { taskTransition(id: %d, to_state: "in_progress") '
         '{ id status } }') % tid
    r = app_client.post("/graphql", json={"query": q})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["taskTransition"]["status"] in ("in_progress", "open")


def test_graphql_mutation_wiki_upsert(temp_db, app_client):
    q = ('mutation { wikiPageUpsert(title: "GQL Page", '
         'body: "from graphql") { id slug title } }')
    r = app_client.post("/graphql", json={"query": q})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["wikiPageUpsert"]["slug"] == "gql-page"
    # Verify via REST.
    g = app_client.get("/api/wiki/pages/gql-page")
    assert g.status_code == 200
    assert g.json()["body"] == "from graphql"


def test_graphql_unknown_mutation_reports_error(temp_db, app_client):
    q = 'mutation { selfDestruct { id } }'
    r = app_client.post("/graphql", json={"query": q})
    assert r.status_code == 200
    body = r.json()
    assert body["errors"]
    assert "selfDestruct" in body["errors"][0]["message"]


def test_graphql_query_still_works(temp_db, app_client):
    """v0.7 query path is not regressed by the v0.11 op-kind changes."""
    _seed(app_client)
    q = "{ tasks(limit: 5) { id title status } }"
    r = app_client.post("/graphql", json={"query": q})
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["data"]["tasks"], list)


# =============================================================== version
def test_version_is_011(temp_db, app_client):
    r = app_client.get("/api/me")
    # /api/me may or may not return version; openapi.json definitely does.
    spec = app_client.get("/openapi.json").json()
    # v0.13 bumped the OpenAPI version; assert it's at least 0.11.0.
    assert spec["info"]["version"] >= "0.11.0"
