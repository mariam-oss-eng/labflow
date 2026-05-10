# HTMX task list & SDK generator · v0.15

## `/app/tasks` — server-rendered, HTMX-powered

A single page with no build step:

* Filter-as-you-type by title (`hx-trigger="keyup changed delay:200ms"`)
* Filter by status (open / in_progress / done / blocked)
* One-click status transitions (`hx-post → outerHTML swap`)

Three endpoints back the page:

```
GET  /app/tasks?q=&status=         # full HTML page
GET  /api/tasks/_table?q=&status=  # just the <table> HTMX swaps in
POST /api/tasks/_status/{id}?to=X  # change status, return refreshed row
```

`htmx@2` is loaded from a CDN with an SRI hash. No bundler, no package.json,
no client framework. The fragment endpoints return `text/html` because
that's what htmx swaps natively.

See [ADR-0023](adr/0023-htmx-task-list.md).

---

## `labflow gen-sdk` — pure-stdlib OpenAPI client generator

LabFlow ships its own client generator. No `openapi-generator-cli` dep,
no Java, no jar.

```bash
# From a saved spec
labflow gen-sdk --spec openapi.json --out client.py

# From this process — no need to run a server first
labflow gen-sdk --from-server --out client.py
```

The output is **one self-contained Python file**:

* depends only on `urllib` + `json`
* one `Client` class with one method per `operationId`
* methods return `dict | list | None` — no Pydantic models, no codegen
  for response schemas; pull whatever you need from the dict

```python
from client import Client
c = Client("https://labflow.example.com", api_key="lf_...")

tasks = c.list_tasks(query={"limit": 20})
me = c.get_task(42)
c.update_task(42, body={"status": "done"})
```

Errors raise a tiny `LabflowError(status, body)` exception. Tests
verify the generated source compiles and exports the expected methods
for every operation in the spec.

### Why a hand-rolled generator?

* **Zero install cost** — `pip install labflow` already gives you it.
* **One file, no transitive deps** — the generated client is auditable
  in 90 lines.
* **Language ecosystems are first-class** — the same shape is what we
  ship in `sdk/typescript/` for the TS client; Python now matches.
