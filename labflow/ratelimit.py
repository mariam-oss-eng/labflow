"""Per-team token-bucket rate limiting (v0.4).

Why an in-process bucket instead of Redis? — At LabFlow's scale a single
API instance serves thousands of teams comfortably; the bucket map is
~80 bytes per team and a process restart is a free reset. When teams
deploy multiple replicas they should put a real edge limiter (Cloudflare,
nginx ``limit_req``, etc.) in front; this middleware is the
defense-in-depth layer that protects each instance from runaway clients.

The bucket is a textbook leaky-token-bucket: every request consumes one
token; tokens regenerate at ``rate_limit_per_minute / 60`` per second up
to a burst cap of ``rate_limit_burst``. We attach standard
``X-RateLimit-*`` and ``Retry-After`` headers to every response.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from .config import get_settings


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-team token bucket. Falls back to per-IP for unauthenticated routes.

    The team identity is read from ``request.state.team`` which the
    auth dependency populates *after* this middleware runs — so for the
    very first request we'll bucket by IP and "promote" to per-team on
    subsequent calls. For API key flows the auth check happens inside
    the dependency tree, well before the response is built, so this is
    fine in practice.
    """

    EXEMPT_PATHS = (
        "/healthz", "/readyz", "/metrics", "/", "/app", "/static",
        "/docs", "/openapi.json", "/redoc",
    )

    def __init__(self, app):
        super().__init__(app)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def _settings_snapshot(self):
        s = get_settings()
        per_min = max(0, int(getattr(s, "rate_limit_per_minute", 0) or 0))
        burst = max(1, int(getattr(s, "rate_limit_burst", 60) or 60))
        return per_min, burst

    def _identity(self, request: Request) -> str:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            # First 16 chars of the key are unique enough as a bucket id;
            # we never store the full plaintext.
            return f"key:{auth[7:23]}"
        x_key = request.headers.get("x-labflow-key")
        if x_key:
            return f"key:{x_key[:16]}"
        client = request.client.host if request.client else "anon"
        return f"ip:{client}"

    async def dispatch(self, request: Request, call_next):
        per_min, burst = self._settings_snapshot()
        if per_min <= 0:
            return await call_next(request)
        path = request.url.path
        if any(path == p or path.startswith(p + "/") for p in self.EXEMPT_PATHS):
            return await call_next(request)

        ident = self._identity(request)
        rate_per_sec = per_min / 60.0
        now = time.monotonic()
        with self._lock:
            b = self._buckets.get(ident)
            if b is None:
                b = _Bucket(tokens=float(burst), updated_at=now)
                self._buckets[ident] = b
            elapsed = max(0.0, now - b.updated_at)
            b.tokens = min(float(burst), b.tokens + elapsed * rate_per_sec)
            b.updated_at = now
            if b.tokens < 1.0:
                retry_after = max(1, int((1.0 - b.tokens) / rate_per_sec) + 1)
                resp = JSONResponse(
                    status_code=429,
                    content={
                        "error": {
                            "code": "rate_limited",
                            "message": "rate limit exceeded",
                            "details": {
                                "limit_per_minute": per_min,
                                "burst": burst,
                                "retry_after_seconds": retry_after,
                            },
                            "request_id": request.headers.get("x-request-id", ""),
                        }
                    },
                )
                resp.headers["Retry-After"] = str(retry_after)
                resp.headers["X-RateLimit-Limit"] = str(per_min)
                resp.headers["X-RateLimit-Remaining"] = "0"
                return resp
            b.tokens -= 1.0
            remaining = int(b.tokens)

        response: Response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(per_min)
        response.headers["X-RateLimit-Remaining"] = str(max(0, remaining))
        return response
