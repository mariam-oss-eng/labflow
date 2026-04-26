"""Bidirectional WebSocket channel (v0.7).

Where SSE is fire-hose-only, the WebSocket lets a client also send back:

* ``{"type": "subscribe", "events": ["task.closed", "comment.added"]}``
* ``{"type": "unsubscribe", "events": [...]}``
* ``{"type": "ping"}`` → ``{"type": "pong", "ts": ...}``

The same in-process ``sse.Hub`` feeds both transports — there's exactly
one fan-out point, so SSE clients and WS clients always see the same
event stream. Dropping in Postgres ``LISTEN/NOTIFY`` or Redis pub/sub
later only requires swapping :func:`labflow.sse.hub`.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Iterable

from . import sse as sse_mod

log = logging.getLogger("labflow.ws")

_HEARTBEAT_S = 20.0


def _filter_pass(event: str, allowed: set[str] | None) -> bool:
    """Return True when ``event`` should be delivered. ``None`` = wildcard."""
    if not allowed:
        return True
    if event in allowed:
        return True
    # Allow ``"task.*"``-style prefixes.
    return any(p.endswith("*") and event.startswith(p[:-1]) for p in allowed)


async def serve(websocket, *, team_id: int) -> None:
    """Run the WS protocol for a single connection.

    Expects the underlying ASGI websocket to already be ``accept()``ed by
    the caller (FastAPI does this for us).
    """
    hub = sse_mod.hub()
    queue = await hub.subscribe(team_id)
    allowed: set[str] | None = None  # None = subscribe to all

    await websocket.send_text(json.dumps({
        "type": "hello", "team_id": team_id,
    }))

    async def _receiver() -> None:
        nonlocal allowed
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except Exception:
                    await websocket.send_text(json.dumps({
                        "type": "error", "error": "invalid json",
                    }))
                    continue
                kind = msg.get("type")
                if kind == "subscribe":
                    events = msg.get("events") or []
                    if not isinstance(events, list):
                        continue
                    allowed = set(allowed or set()) | {str(e) for e in events}
                elif kind == "unsubscribe":
                    events = msg.get("events") or []
                    if allowed is not None:
                        allowed -= {str(e) for e in events}
                        if not allowed:
                            allowed = None
                elif kind == "ping":
                    await websocket.send_text(json.dumps({
                        "type": "pong",
                        "ts": msg.get("ts"),
                    }))
        except Exception:  # noqa: BLE001 — connection closed or torn down
            log.debug("ws receiver done", exc_info=True)

    async def _sender() -> None:
        try:
            while True:
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_S)
                except asyncio.TimeoutError:
                    await websocket.send_text(json.dumps({"type": "heartbeat"}))
                    continue
                if not _filter_pass(evt.event, allowed):
                    continue
                await websocket.send_text(json.dumps({
                    "type": "event",
                    "event": evt.event,
                    "data": evt.data,
                }))
        except Exception:  # noqa: BLE001
            log.debug("ws sender done", exc_info=True)

    recv = asyncio.create_task(_receiver())
    send = asyncio.create_task(_sender())
    try:
        done, pending = await asyncio.wait(
            {recv, send}, return_when=asyncio.FIRST_COMPLETED,
        )
        for p in pending:
            p.cancel()
    finally:
        await hub.unsubscribe(team_id, queue)
