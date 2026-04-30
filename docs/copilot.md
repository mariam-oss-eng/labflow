# AI Copilot (v0.9)

A multi-step **tool-using agent** that runs against your team's data.
Designed to be useful **offline by default** (no LLM required) via a
deterministic rule-based planner, with a clean hook for an LLM backend
so production deployments can swap in GPT-4 / Claude / a local model.

See [ADR-0009](adr/0009-copilot-tool-agent.md) for the design rationale.

## Tools

| Tool | Description |
|---|---|
| `search(query)` | Hybrid keyword/semantic search over decisions + tasks |
| `get_task(task_id)` | Fetch a task plus comments and evidence count |
| `list_open_tasks(owner=None)` | Filter open tasks by optional owner handle |
| `list_decisions(limit=N)` | Recent decisions for the team |
| `summarize_meeting(meeting_id)` | Stored summary or rule-based fallback |
| `propose_task(title, owner)` | **Draft only** — never auto-creates |
| `analytics(days=N)` | Cycle-time / throughput / completion rate |

`propose_task` is intentionally side-effect-free. The agent never
mutates state on its own; explicit user actions (a `POST /api/meetings`
or `POST /api/tasks/{id}/transition`) are the only writers.

## Sessions

```bash
curl -X POST http://localhost:8000/api/copilot/sessions \
  -H 'Content-Type: application/json' \
  -d '{"title":"Why is the cluster behind?"}'

# {"id": 1, "title": "Why is the cluster behind?", "closed": false, ...}

curl -X POST http://localhost:8000/api/copilot/sessions/1/turns \
  -H 'Content-Type: application/json' \
  -d '{"message":"show me open tasks"}'
```

The reply contains:
* `reply` — the assistant message (Markdown).
* `tool_calls` — the structured tool invocations and their results.
* `session_id` — for follow-up turns.

The full transcript (every user message, every tool call, every reply)
is persisted in `copilot_sessions.transcript_json`. Every turn also
writes a `copilot.turn` audit event so compliance review is just SQL.

## Plugging in an LLM

Set:

```bash
export LABFLOW_COPILOT_LLM_CALLABLE=my_llm_module:answer
```

…where `answer(messages, tools) -> str` either returns a plain
assistant message or a JSON string of the form
`{"tool": "name", "arguments": {...}}`. Anything malformed falls back
to the deterministic planner.

## Discovering the schema

`GET /api/copilot/tools` returns the full tool list as JSON Schema-ish
descriptors so an external LLM can self-describe its capabilities.
