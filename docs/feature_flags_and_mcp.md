# Feature flags & MCP tool endpoint · v0.14

## Feature flags

Per-team, persisted, with optional JSON payload. Reads go through a
1-second process cache so a hot route can call `is_enabled` per
request without hitting the DB.

```python
from labflow import feature_flags as ff

if ff.is_enabled(sess, team_id=team.id, key="copilot.beta"):
    payload = ff.payload(sess, team_id=team.id, key="copilot.beta")
    variant = (payload or {}).get("variant", "A")
```

### Endpoints

```
GET    /api/feature-flags
PUT    /api/feature-flags/{key}    {enabled: bool, payload?: {...}}
DELETE /api/feature-flags/{key}
```

Keys must match `[a-z0-9._-]{1..80}` — validated on every write so
bad names never reach the DB.

See [ADR-0020](adr/0020-feature-flags.md).

---

## MCP-style JSON-RPC tool endpoint

`POST /api/mcp` is a JSON-RPC 2.0 endpoint exposing **read-only** tools
to external LLM agents that already speak the
[Model Context Protocol](https://modelcontextprotocol.io/).

Two methods:

```
POST /api/mcp   {"jsonrpc":"2.0","id":1,"method":"tools/list"}
POST /api/mcp   {"jsonrpc":"2.0","id":2,"method":"tools/call",
                 "params":{"name":"list_open_tasks","arguments":{"limit":10}}}
```

A `tools/list` response:

```json
{
  "jsonrpc": "2.0", "id": 1,
  "result": {
    "tools": [
      {"name": "search", "description": "Hybrid keyword/semantic search...",
       "input_schema": {"type":"object","required":["query"], ...}},
      ...
    ]
  }
}
```

A `tools/call` response carries the structured result inside an MCP
content block:

```json
{
  "jsonrpc": "2.0", "id": 2,
  "result": {
    "isError": false,
    "content": [{"type":"json","json":{"tasks":[{"id":1,"title":"..."}]}}]
  }
}
```

### What's in the registry

| Tool | Purpose |
|---|---|
| `search`           | Hybrid keyword/semantic search across team data. |
| `list_open_tasks`  | Open tasks ordered by due date. |
| `get_task`         | Fetch one task by id. |
| `list_decisions`   | Recent decisions. |
| `analytics`        | Team analytics roll-up over the last N days. |

Mutations stay on the typed REST API where they get audit, ACL, and
idempotency for free.

See [ADR-0021](adr/0021-mcp-tool-endpoint.md).

---

## Smart-list change subscriptions

`POST /api/smart-lists/{slug}/subscriptions  {webhook_url, secret?}`

A periodic sweeper runs each subscribed list and only fires when the
SHA-256 digest of the resulting task IDs differs from the previous
fire — sweeps with no list changes are no-ops.

```
POST /api/admin/smart-lists/sweep
```

returns `{"fired": [{subscription_id, smart_list_slug, webhook_url,
secret, task_ids, digest, ...}]}` — delivery is intentionally
decoupled from detection so the caller can wire any transport.
