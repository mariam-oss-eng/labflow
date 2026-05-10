# ADR-0021: A minimal MCP-style JSON-RPC tool endpoint

* **Status**: accepted (v0.14)
* **Context**: External LLM agents — Claude, GPT, Gemini, local
  models — increasingly speak the
  [Model Context Protocol](https://modelcontextprotocol.io/). We
  already have a tool-using AI Copilot (`labflow.copilot`) that picks
  tools server-side. We do **not** want every external agent to first
  spin up a copilot session — that's the wrong granularity.
* **Decision**: Expose a tiny JSON-RPC 2.0 endpoint at `POST /api/mcp`
  with two methods:
  * `tools/list` — returns the tool registry with input schemas.
  * `tools/call` — invokes one tool by name.
  Tools are **read-only**. Mutations stay behind the typed REST API
  where every change goes through audit + ACL.

## Why JSON-RPC 2.0 over a REST shape

MCP itself is JSON-RPC. Speaking the wire format an external agent
already knows means a one-line adapter, not a translation layer.

## Why read-only

A free-form tool call from an LLM should never be able to mutate state
without the user seeing the side effect. Mutations land through a
named REST endpoint that already has an OpenAPI shape, ACL checks,
audit chain entries, and idempotency support. If a tool needs to
mutate, it's a regular REST call with an API key.

## What's in the registry today

| Tool | Purpose |
|---|---|
| `search`           | Hybrid keyword/semantic search across the team's data. |
| `list_open_tasks`  | List open tasks ordered by due date. |
| `get_task`         | Fetch one task by id. |
| `list_decisions`   | Recent decisions (statement + rationale). |
| `analytics`        | Team analytics roll-up over the last N days. |

Adding a tool is one entry in `mcp._TOOLS`. The handler signature is
`(sess, *, team_id: int, args: dict) -> dict` — same as the rest of
the codebase.
