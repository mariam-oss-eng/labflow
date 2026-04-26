# Realtime: Server-Sent Events + WebSocket

LabFlow streams every state change through an in-process **Hub** that is
fanned out over two transports: SSE for fire-hose, WebSocket for
filtered bidirectional flows. See [ADR-0005](adr/0005-websocket-vs-sse.md)
for the rationale.

## Event catalog

| Event | Payload |
| --- | --- |
| `meeting.finalized` | `{ meeting_id, title }` |
| `task.closed` | `{ task_id, title }` |
| `evidence.verified` | `{ task_id, score }` |
| `comment.added` | `{ comment_id, entity_type, entity_id, actor }` |
| `webhook.delivered` | `{ event, attempt, status }` |

Plugins can publish their own events via `labflow.sse.hub().publish(team_id, event, data)`.

## Server-Sent Events (`GET /api/stream`)

```bash
curl -N https://labflow.example.com/api/stream \
  -H "Authorization: Bearer $LABFLOW_API_KEY"
```

Each event has the standard SSE shape (`event:`, `data:` lines). The
browser's `EventSource` reconnects automatically; a 15s heartbeat keeps
intermediate proxies from timing out.

## WebSocket (`/ws`)

Bidirectional. Clients can send the following control messages:

```json
{"type": "subscribe",   "events": ["task.closed", "comment.added"]}
{"type": "unsubscribe", "events": ["comment.added"]}
{"type": "subscribe",   "events": ["task.*"]}     // prefix wildcard
{"type": "ping",        "ts": 1714137600}         // → {"type":"pong","ts":...}
```

Server messages:

```json
{"type": "hello",     "team_id": 1}
{"type": "event",     "event": "task.closed", "data": {"task_id": 42}}
{"type": "heartbeat"}
```

### Browser

```javascript
const ws = new WebSocket("wss://labflow.example.com/ws?token=lfk_...");
ws.onmessage = (m) => console.log(JSON.parse(m.data));
ws.onopen = () => ws.send(JSON.stringify({
  type: "subscribe", events: ["task.*", "evidence.verified"],
}));
```

### Python

```python
import asyncio, json, websockets
async def main():
    async with websockets.connect("wss://.../ws?token=lfk_...") as ws:
        await ws.send(json.dumps({"type": "subscribe", "events": ["task.closed"]}))
        async for raw in ws:
            print(json.loads(raw))
asyncio.run(main())
```

## Auth

* **SSE** uses the standard `Authorization: Bearer …` header.
* **WS** uses a `?token=…` query param because browsers can't set
  custom headers on the WebSocket handshake. In single-team mode no
  token is required.
* In both transports, every event a connection sees is already team-scoped
  — there is no cross-tenant leakage even with permissive subscriptions.
