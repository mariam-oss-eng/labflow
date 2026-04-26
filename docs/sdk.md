# Python SDK (`labflow_client`)

The official client for the LabFlow REST + GraphQL API. Sync and async
flavors. Uses `httpx` when available, falls back to the stdlib so it
works in lightweight runtimes (AWS Lambda, restricted CI images).

## Install

```bash
# Comes bundled with the main package
pip install labflow

# Or just the client (no FastAPI deps pulled in)
pip install labflow-client
```

## Sync

```python
from labflow_client import LabFlow

lf = LabFlow("https://labflow.example.com", api_key="lfk_...")

# Health
assert lf.health()["ok"]

# Create + extract a meeting
m = lf.create_meeting(
    title="Sprint planning",
    transcript=open("notes.md").read(),
    idempotency_key="sp-2026-04-26",
)
print(lf.summarize_meeting(m["id"], max_sentences=3)["summary"])

# Pull tasks and add evidence
for t in lf.list_tasks(status="open")["items"]:
    if t["title"].lower().startswith("ship"):
        lf.add_evidence(task_id=t["id"], kind="commit",
                        uri="https://github.com/acme/repo/commit/abc",
                        summary="Feature deployed")

# Hybrid search + decision graph
hits = lf.search("retrieval latency", limit=5)
graph = lf.decision_graph()

# Analytics + GraphQL
print(lf.analytics(days=14)["completion_rate"])
print(lf.graphql("{ team { slug } analytics(days:7){ tasks_closed } }"))

# Collaboration
lf.add_comment(entity_type="task", entity_id=42, body="Added the dataset")
lf.react(entity_type="decision", entity_id=7, emoji="👍")

lf.close()
```

## Async

```python
import asyncio
from labflow_client import AsyncLabFlow

async def main():
    async with AsyncLabFlow("https://labflow.example.com",
                            api_key="lfk_...") as lf:
        tasks = await lf.list_tasks()
        print(len(tasks["items"]))

asyncio.run(main())
```

The async client requires `httpx`. Install with `pip install httpx`.

## Errors

Every non-2xx response raises `LabFlowError`:

```python
from labflow_client import LabFlowError
try:
    lf.create_meeting(title="", transcript="")
except LabFlowError as exc:
    print(exc.status, exc.body)
```

## Idempotency

Pass `idempotency_key=` on any write to participate in LabFlow's
server-side replay cache. Identical keys within 24h return the cached
response instead of double-creating.

## Notes

* The sync client transparently uses `httpx` when installed (gives you
  HTTP/2 + connection pooling for free), falling back to `urllib`
  otherwise.
* All methods return parsed JSON dicts/lists. Binary endpoints
  (`/api/calendar.ics`) return strings.
* The SDK does **not** wrap the WebSocket endpoint — point an `await
  websockets.connect(...)` directly at `/ws?token=...`.
