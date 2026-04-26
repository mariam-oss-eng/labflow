# ADR-0005 · Keep both SSE and WebSocket; share a single hub

* Status: Accepted
* Date: 2026-04-26 (v0.7)

## Context

v0.5 shipped Server-Sent Events for live updates. SSE is excellent at
server-push (no protocol upgrade, traverses corporate proxies, automatic
reconnect by the browser) but **it is one-way**. v0.7 adds two needs:

1. The dashboard wants per-tab event filtering (e.g. "only stream
   `comment.added` for this entity"), which means clients have to send
   subscription preferences back to the server.
2. CLI/SDK use cases want a real bidirectional channel for ping/pong
   liveness and for upcoming features (collaborative cursors, presence).

## Decision

We add a `/ws` WebSocket endpoint **alongside** the existing
`/api/stream` SSE endpoint. They share the in-process `sse.Hub`:

* `Hub.publish(team_id, event, data)` is the single fan-out call.
* The SSE route iterates `Hub.subscribe()` directly.
* The WS route runs a small protocol over `Hub.subscribe()`, applying
  the connection's filter set before forwarding.

Both transports authenticate with the same API key: SSE via the
`Authorization` header (proxy-friendly), WS via a `?token=` query param
(browsers can't set headers on the WS handshake).

## Why not pick one

* **WS-only**: breaks `curl -N`, breaks dumb HTTP intermediaries that
  haven't been upgraded for WebSocket framing, and forces the browser
  reconnect logic into our code.
* **SSE-only**: no upstream channel — every "subscribe" or "ping" would
  need a sibling REST request, doubling round trips.

## Future migration

If multi-replica HA traffic exceeds the in-process hub, swap
`labflow.sse.Hub` to a Postgres `LISTEN/NOTIFY` or Redis Pub/Sub
implementation. The route handlers and the SDK don't change.
