# ADR-0002 · Idempotency as raw ASGI middleware

* Status: Accepted
* Date: 2026-04-25 (v0.4)

## Context

We want Stripe-style `Idempotency-Key` semantics on all writes:

* The first request runs normally; status, body, and a SHA-256 of the
  request body are persisted.
* Subsequent identical requests replay the cached response.
* Subsequent requests with the *same* key but a *different* body get
  `409 Conflict`.

The implementation has to (a) read the request body before the route
handler does and re-feed it downstream, and (b) capture the response
body after the handler emits it.

## Decision

Implement the middleware as **raw ASGI** rather than a subclass of
Starlette's `BaseHTTPMiddleware`. Concretely we write a class with an
`async __call__(self, scope, receive, send)` method, wrap `send` to
capture `http.response.start` + `http.response.body` events, and
substitute `receive` so the route handler still sees the original body.

We considered three alternatives:

1. **`BaseHTTPMiddleware`.** Cleaner API, but consuming
   `response.body_iterator` after `await call_next(request)` triggers
   a [well-documented 5-second hang](https://github.com/encode/starlette/issues/1438)
   when the inner streaming task hasn't finished — we observed exactly
   that locally (every write took 5.0s instead of 5–20ms).
2. **Per-route dependency.** Doesn't see the response body, only the
   request. Would require every endpoint to opt-in by hand.
3. **Reverse proxy.** Pushes the problem out of the app and forces
   every operator to install nginx + Lua or similar. Operationally
   heavy.

Persistence is SQL (`idempotency_records` table) for the same reason
the rest of LabFlow avoids Redis: zero extra moving parts. The
retention sweep prunes expired rows.

## Consequences

* **Positive** — sub-millisecond replay on cache hits; the rest of
  the system is untouched (route handlers don't even know idempotency
  is happening).
* **Negative** — raw ASGI middleware is more verbose and has fewer
  ergonomic helpers than `BaseHTTPMiddleware`. We mitigate this by
  keeping the file under 200 lines and isolating the wire-format work
  in two private helpers.
* **Follow-on** — when we move to Postgres-only deployments, we'll
  swap the table for an unlogged table to drop the WAL cost on what
  is essentially ephemeral data.
