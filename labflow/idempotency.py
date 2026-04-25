"""``Idempotency-Key`` support for ``POST`` endpoints (v0.4).

Implementation note — written as a **raw ASGI middleware** instead of
``BaseHTTPMiddleware`` because we need to (a) read the request body
*before* the route handler does, and (b) capture the response body
*after* the route handler emits it. ``BaseHTTPMiddleware``'s
body-iterator integration doesn't compose cleanly with that; raw ASGI
avoids the well-documented `5-second hang <https://github.com/encode/
starlette/issues/1438>`_ around ``response.body_iterator`` consumption.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from sqlalchemy import select

from . import models
from .config import get_settings
from .db import get_session_factory
from .time_utils import now_utc

_HEADER = b"idempotency-key"
_WRITE_METHODS = {"POST", "PUT", "PATCH"}


def _digest(method: str, path: str, body: bytes) -> str:
    h = hashlib.sha256()
    h.update(method.encode())
    h.update(b"\x00")
    h.update(path.encode())
    h.update(b"\x00")
    h.update(body)
    return h.hexdigest()


def _resolve_team_id(headers: dict[bytes, bytes], sess) -> int | None:
    settings = get_settings()
    if not settings.auth_enabled:
        team = sess.execute(
            select(models.Team).where(models.Team.slug == settings.bootstrap_team)
        ).scalar_one_or_none()
        return team.id if team is not None else None
    auth = headers.get(b"authorization", b"").decode("latin-1", errors="replace")
    plaintext = ""
    if auth.lower().startswith("bearer "):
        plaintext = auth[7:].strip()
    if not plaintext:
        plaintext = headers.get(b"x-labflow-key", b"").decode(
            "latin-1", errors="replace"
        ).strip()
    if not plaintext:
        return None
    from .auth import hash_api_key
    key = sess.execute(
        select(models.ApiKey).where(
            models.ApiKey.key_hash == hash_api_key(plaintext),
            models.ApiKey.revoked_at.is_(None),
        )
    ).scalar_one_or_none()
    return key.team_id if key is not None else None


def _error_response(status: int, code: str, message: str) -> tuple[int, dict, bytes]:
    body = json.dumps({
        "error": {"code": code, "message": message,
                  "details": None, "request_id": ""}
    }).encode("utf-8")
    return status, {"content-type": "application/json"}, body


class IdempotencyMiddleware:
    """Raw ASGI middleware. Wraps the inner ``send`` to capture response
    bytes; reads the request body once and re-feeds it to downstream."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        method = scope.get("method", "GET")
        if method not in _WRITE_METHODS:
            return await self.app(scope, receive, send)

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        key_bytes = headers.get(_HEADER)
        if not key_bytes:
            return await self.app(scope, receive, send)
        key = key_bytes.decode("latin-1", errors="replace")
        if len(key) > 128:
            return await self._send_error(
                send, *_error_response(400, "validation_error",
                                       "Idempotency-Key too long (max 128)")
            )

        # Drain request body so we can hash + replay it.
        body = b""
        while True:
            message = await receive()
            if message["type"] != "http.request":
                continue
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break

        path = scope.get("path", "")
        digest = _digest(method, path, body)
        SessionLocal = get_session_factory()
        team_id = None
        with SessionLocal() as s:
            team_id = _resolve_team_id(headers, s)
            if team_id is not None:
                rec = s.execute(
                    select(models.IdempotencyRecord).where(
                        models.IdempotencyRecord.team_id == team_id,
                        models.IdempotencyRecord.key == key,
                    )
                ).scalar_one_or_none()
                if rec is not None:
                    if rec.request_hash != digest:
                        return await self._send_error(
                            send, *_error_response(
                                409, "idempotency_mismatch",
                                ("Idempotency-Key reused with a different "
                                 "request body."),
                            )
                        )
                    return await self._replay(send, rec)

        # Re-feed the body to the inner app exactly once.
        body_sent = False

        async def replay_receive():
            nonlocal body_sent
            if body_sent:
                return await receive()
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        # Capture the response on the way out.
        captured_status: int | None = None
        captured_headers: list[tuple[bytes, bytes]] = []
        captured_body = bytearray()

        async def wrap_send(message):
            nonlocal captured_status, captured_headers
            if message["type"] == "http.response.start":
                captured_status = message["status"]
                captured_headers = list(message.get("headers", []))
            elif message["type"] == "http.response.body":
                captured_body.extend(message.get("body", b""))
            await send(message)

        await self.app(scope, replay_receive, wrap_send)

        if (team_id is None or captured_status is None
                or not (200 <= captured_status < 300)):
            return

        ttl = int(getattr(get_settings(), "idempotency_ttl_seconds", 24 * 3600))
        ctype = "application/json"
        for hk, hv in captured_headers:
            if hk.lower() == b"content-type":
                ctype = hv.decode("latin-1", errors="replace")
                break
        with SessionLocal() as s:
            try:
                s.add(models.IdempotencyRecord(
                    team_id=team_id, key=key, request_hash=digest,
                    method=method, path=path,
                    status_code=captured_status,
                    response_body=bytes(captured_body).decode(
                        "utf-8", errors="replace"),
                    response_content_type=ctype,
                    expires_at=(now_utc() + timedelta(seconds=ttl))
                                .replace(tzinfo=None),
                ))
                s.commit()
            except Exception:  # noqa: BLE001
                s.rollback()

    async def _send_error(self, send, status, headers, body):
        await send({
            "type": "http.response.start", "status": status,
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()]
                       + [(b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body, "more_body": False})

    async def _replay(self, send, rec):
        body = rec.response_body.encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": rec.status_code,
            "headers": [
                (b"content-type", rec.response_content_type.encode("latin-1")),
                (b"content-length", str(len(body)).encode()),
                (b"idempotent-replay", b"true"),
            ],
        })
        await send({"type": "http.response.body", "body": body, "more_body": False})

