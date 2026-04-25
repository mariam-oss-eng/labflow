"""Server-Sent Events for live dashboard updates (v0.5).

SSE is the right tool for "push events to the browser" when the events
are unidirectional and infrequent (a dozen per minute, not a thousand).
It avoids the operational complexity of WebSockets — works through every
HTTP/1.1 proxy, reconnects automatically with ``Last-Event-ID``, no
extra dependency.

Implementation notes
--------------------
* The hub is **in-process** — multiple API replicas don't see each
  other's events. That's fine because the hub is fed by the same
  request-handling thread that just persisted the change. If you need
  cross-replica fan-out, swap in Postgres ``LISTEN/NOTIFY`` or Redis
  pub/sub at the ``Hub.publish`` boundary.
* Streams are scoped per-team so a subscriber only ever sees events for
  its own workspace.
* We send a heartbeat comment every 15s to keep intermediate proxies
  from closing the connection.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import AsyncIterator

log = logging.getLogger("labflow.sse")

_HEARTBEAT_SECONDS = 15.0


@dataclass
class Event:
    event: str
    data: dict


class Hub:
    """Per-team in-memory pub/sub fan-out."""

    def __init__(self):
        self._subs: dict[int, list[asyncio.Queue]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def subscribe(self, team_id: int) -> asyncio.Queue:
        async with self._lock:
            q: asyncio.Queue = asyncio.Queue(maxsize=64)
            self._subs[team_id].append(q)
            return q

    async def unsubscribe(self, team_id: int, q: asyncio.Queue) -> None:
        async with self._lock:
            try:
                self._subs[team_id].remove(q)
            except ValueError:
                pass
            if not self._subs[team_id]:
                self._subs.pop(team_id, None)

    def publish(self, team_id: int, event: str, data: dict) -> None:
        """Fire-and-forget publish. Drops events for slow subscribers
        rather than blocking the request thread."""
        evt = Event(event=event, data=data)
        for q in list(self._subs.get(team_id, [])):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                log.warning("dropping SSE event for slow subscriber team=%s", team_id)


_hub = Hub()


def hub() -> Hub:
    return _hub


async def stream(team_id: int) -> AsyncIterator[bytes]:
    """Async generator yielding SSE-formatted bytes for a team."""
    q = await _hub.subscribe(team_id)
    try:
        # Send a hello event right away so the client knows it's connected.
        yield _format("hello", {"team_id": team_id})
        while True:
            try:
                evt = await asyncio.wait_for(q.get(), timeout=_HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                # Comment-only line keeps the connection alive.
                yield b": heartbeat\n\n"
                continue
            yield _format(evt.event, evt.data)
    finally:
        await _hub.unsubscribe(team_id, q)


def _format(event: str, data: dict) -> bytes:
    payload = json.dumps(data, default=str)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
