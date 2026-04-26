# Collaboration

v0.6 turns LabFlow's typed graph into a *social* artifact: every
decision and task can host a threaded discussion and a set of emoji
reactions, and every team can save its searches. See
[ADR-0004](adr/0004-collaboration-model.md) for the data model.

## Comments

Threaded, soft-deletable, audit-logged. Replies form a tree via
`parent_id`. The list endpoint returns a nested forest; the create
endpoint accepts a flat row.

```bash
# Add a top-level comment
curl -X POST .../api/comments \
  -H "Authorization: Bearer $LABFLOW_API_KEY" \
  -d '{"entity_type": "task", "entity_id": 42, "body": "Need a benchmark first."}'

# Reply
curl -X POST .../api/comments \
  -d '{"entity_type": "task", "entity_id": 42,
       "parent_id": 17, "body": "+1, will run it Friday."}'

# List
curl .../api/comments?entity_type=task&entity_id=42
```

## Reactions

Toggleable. Posting the same emoji twice removes it. The toggle
response includes the new counts so the UI doesn't need a follow-up
GET.

```bash
curl -X POST .../api/reactions \
  -d '{"entity_type": "decision", "entity_id": 9, "emoji": "👍"}'
# → {"added": true, "counts": {"👍": 3, "🚀": 1}}
```

Allowed emojis are intentionally a small, semantic set: `👍 👎 🎉 ❤️
🚀 👀 🤔 ✅`. Adding more is a one-line change in `labflow/collab.py`.

## Saved searches

A saved search is a named, persistent `/api/search` query bound to a
team. Slugs auto-derive from the name; pinned searches surface at the
top of the dashboard sidebar.

```bash
curl -X POST .../api/saved-searches \
  -d '{"name":"Postgres mentions","query":"postgres","alpha":0.4,"pinned":true}'

curl .../api/saved-searches/postgres-mentions/run
```

## Notification preferences

Each API key has its own preferences row, lazy-created on first read:

```bash
curl .../api/me/notifications
# → {"digest_cadence":"weekly","email":null,"muted_events":[]}

curl -X PUT .../api/me/notifications \
  -d '{"digest_cadence":"daily","email":"alice@lab.example",
       "muted_events":["evidence.verified"]}'
```

The HTML digest renderer at `GET /api/digest/weekly.html` honors
`muted_events` and includes only the sections the user hasn't muted.
