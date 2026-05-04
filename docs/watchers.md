# Watchers & activity feed

Want to know when *that one task* moves? Subscribe to it and read
your personal feed. v0.11 ships per-API-key watchers and an
aggregated `/api/feed` endpoint built on top of the audit log.

## Watching an entity

```bash
curl -X POST -H "Content-Type: application/json" -H "X-API-Key: $KEY" \
     -d '{"entity_type": "task", "entity_id": 4127, "delivery": "feed"}' \
     https://labflow.example/api/watchers
```

Supported `entity_type` values: `task`, `decision`, `meeting`, `wiki`,
`experiment`, `sprint`, `blocker`, `assumption`, `evidence`.

Supported `delivery` values: `feed` (read at `/api/feed`), `email`
(out-of-band delivery hook — wire to your provider), `slack` (same).

## Listing and removing

```bash
curl -H "X-API-Key: $KEY" \
     https://labflow.example/api/watchers

curl -X DELETE -H "X-API-Key: $KEY" \
     "https://labflow.example/api/watchers?entity_type=task&entity_id=4127"
```

## Reading the feed

```bash
curl -H "X-API-Key: $KEY" \
     "https://labflow.example/api/feed?limit=50"
```

```json
{
  "events": [
    {"id": 9821, "actor": "alice", "action": "task.transition",
     "entity_type": "task", "entity_id": 4127,
     "created_at": "2026-05-04T18:11:09",
     "metadata_json": "{\"from\":\"open\",\"to\":\"in_progress\"}"},
    ...
  ]
}
```

The feed is computed at read time by joining `watchers` and
`audit_events` — no extra persistence, no risk of drift between the
audit log and the notification stream. If the calling key has zero
watches, the endpoint returns the **team-wide** audit log so admins
get a full activity stream without bookkeeping.

## Combining with the audit chain

Because `/api/feed` reads straight from `audit_events`, every event
shown in the feed is part of the tamper-evident hash chain (see
[Audit chain & verification](audit_chain.md)). A row in your feed is
cryptographic evidence the event happened.

## Rate / volume

Each watch costs one row in the `watchers` table. The feed query is
a single indexed `SELECT` per request; the underlying join is
`(team_id, entity_type, entity_id)` over a covering index, so
hundreds of watches per key remain instant.
