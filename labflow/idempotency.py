"""``Idempotency-Key`` support for ``POST`` endpoints (v0.4).

When a client retries a write (network blip, mobile flake, lambda
re-invocation) we don't want to create a duplicate meeting / job /
evidence row. Stripe's idempotency contract is the de-facto standard:

  * The client sends a unique ``Idempotency-Key`` header (UUID is fine).
  * The first request runs normally; the response status, body, and a
    SHA-256 of the request body are persisted in
    :class:`models.IdempotencyRecord`.
  * Subsequent requests with the same key replay the cached response if
    the body hash matches; if it differs we return ``409 Conflict`` so
    the client realizes their key→payload mapping is wrong.
  * Records expire after ``LABFLOW_IDEMPOTENCY_TTL_SECONDS`` (default 24h).

We deliberately persist in SQL (vs. Redis) for the same reasons as the
job queue — operational simplicity. A janitor job can prune expired
rows; nothing else relies on real-time eviction.
"""
from __future__ import annotations

import hashlib
from datetime import timedelta

from fastapi import Request
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from . import models
from .config import get_settings
from .db import get_session_factory
from .time_utils import now_utc

_HEADER = "idempotency-key"


def _digest(method: str, path: str, body: bytes) -> str:
    h = hashlib.sha256()
    h.update(method.encode())
    h.update(b"\x00")
    h.update(path.encode())
    h.update(b"\x00")
    h.update(body)
    return h.hexdigest()


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """Replay ``POST`` requests carrying ``Idempotency-Key``.

    We only enforce idempotency on writes (``POST``/``PUT``/``PATCH``).
    A response is replayed from cache when the request body hash
    matches; otherwise the first body wins and the second errors. We
    only cache 2xx responses — failures should be retryable verbatim.
    """

    METHODS = {"POST", "PUT", "PATCH"}

    async def dispatch(self, request: Request, call_next):
        if request.method not in self.METHODS:
            return await call_next(request)
        key = request.headers.get(_HEADER)
        if not key:
            return await call_next(request)
        if len(key) > 128:
            return JSONResponse(status_code=400, content={
                "error": {"code": "validation_error",
                          "message": "Idempotency-Key too long (max 128)",
                          "details": None,
                          "request_id": request.headers.get("x-request-id", "")}
            })
        team = getattr(request.state, "team", None)
        # We need to read the body to compute the digest, then re-attach
        # it to the request scope so downstream handlers still see it.
        body = await request.body()

        async def _receive():
            return {"type": "http.request", "body": body, "more_body": False}
        request._receive = _receive  # type: ignore[attr-defined]

        digest = _digest(request.method, request.url.path, body)
        SessionLocal = get_session_factory()

        # If we know the team already, look up cached response first.
        cached_team_id = team.id if team is not None else None
        if cached_team_id is not None:
            with SessionLocal() as s:
                rec = s.execute(
                    select(models.IdempotencyRecord).where(
                        models.IdempotencyRecord.team_id == cached_team_id,
                        models.IdempotencyRecord.key == key,
                    )
                ).scalar_one_or_none()
                if rec is not None:
                    if rec.request_hash != digest:
                        return JSONResponse(status_code=409, content={
                            "error": {
                                "code": "idempotency_mismatch",
                                "message": ("Idempotency-Key reused with a "
                                            "different request body."),
                                "details": None,
                                "request_id": request.headers.get(
                                    "x-request-id", ""),
                            }
                        })
                    return Response(
                        content=rec.response_body,
                        status_code=rec.status_code,
                        media_type=rec.response_content_type,
                        headers={"Idempotent-Replay": "true"},
                    )

        response: Response = await call_next(request)
        # After the call, the auth dep should have set request.state.team
        # if it ran. Use that team_id for storage; otherwise we can't store.
        team = getattr(request.state, "team", None)
        if team is None or not (200 <= response.status_code < 300):
            return response

        # Capture the response body. Starlette streams the body; we have
        # to consume the iterator and rebuild a Response that includes it.
        chunks = []
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            chunks.append(chunk)
        raw = b"".join(chunks)

        ttl = int(getattr(get_settings(), "idempotency_ttl_seconds", 24 * 3600))
        with SessionLocal() as s:
            # Last-write-wins on race: if two concurrent identical requests
            # both finish before the other persists, the second insert hits
            # the unique constraint and we silently swallow it.
            try:
                s.add(models.IdempotencyRecord(
                    team_id=team.id, key=key, request_hash=digest,
                    method=request.method, path=request.url.path,
                    status_code=response.status_code,
                    response_body=raw.decode("utf-8", errors="replace"),
                    response_content_type=(response.headers.get("content-type")
                                           or "application/json"),
                    expires_at=(now_utc() + timedelta(seconds=ttl))
                                .replace(tzinfo=None),
                ))
                s.commit()
            except Exception:  # noqa: BLE001 — best-effort cache write
                s.rollback()

        return Response(
            content=raw, status_code=response.status_code,
            media_type=response.media_type,
            headers=dict(response.headers),
        )
