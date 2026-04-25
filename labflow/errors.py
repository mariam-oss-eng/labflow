"""Typed application errors and FastAPI exception handlers.

Goals:
  * Every error response uses the same envelope so SDKs and clients can
    rely on a consistent shape.
  * Domain code raises typed exceptions (``NotFoundError``, ``ConflictError``,
    …) instead of constructing ``HTTPException`` directly. This keeps the
    domain layer framework-agnostic.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("labflow.errors")


class LabFlowError(Exception):
    """Base class for typed application errors.

    Subclasses set ``status_code`` and ``code`` so handlers can produce a
    consistent ``{"error": {...}}`` envelope.
    """

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(LabFlowError):
    status_code = 404
    code = "not_found"


class ConflictError(LabFlowError):
    status_code = 409
    code = "conflict"


class ValidationError(LabFlowError):
    status_code = 400
    code = "invalid_request"


class AuthError(LabFlowError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(LabFlowError):
    status_code = 403
    code = "forbidden"


class RateLimitError(LabFlowError):
    status_code = 429
    code = "rate_limited"


class PayloadTooLargeError(LabFlowError):
    status_code = 413
    code = "payload_too_large"


def _envelope(
    code: str,
    message: str,
    *,
    status_code: int,
    request_id: str | None,
    details: Any = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
        }
    }
    if details is not None:
        body["error"]["details"] = details
    if request_id:
        body["error"]["request_id"] = request_id
    return JSONResponse(body, status_code=status_code)


def install_exception_handlers(app: FastAPI) -> None:
    """Register handlers for typed errors + framework errors."""

    @app.exception_handler(LabFlowError)
    async def _handle_labflow_error(request: Request, exc: LabFlowError):  # noqa: D401
        rid = getattr(request.state, "request_id", None)
        log.info(
            "labflow_error",
            extra={"code": exc.code, "status": exc.status_code, "request_id": rid},
        )
        return _envelope(
            exc.code,
            exc.message,
            status_code=exc.status_code,
            request_id=rid,
            details=exc.details,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(request: Request, exc: StarletteHTTPException):
        rid = getattr(request.state, "request_id", None)
        # Map a few common ones; default to "http_error".
        code = {400: "invalid_request", 401: "unauthorized", 403: "forbidden",
                404: "not_found", 409: "conflict", 413: "payload_too_large",
                429: "rate_limited"}.get(exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) else "request failed"
        return _envelope(code, message, status_code=exc.status_code, request_id=rid)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(request: Request, exc: RequestValidationError):
        rid = getattr(request.state, "request_id", None)
        return _envelope(
            "invalid_request",
            "request validation failed",
            status_code=422,
            request_id=rid,
            details=exc.errors(),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception):
        rid = getattr(request.state, "request_id", None)
        log.exception("unhandled_exception", extra={"request_id": rid})
        return _envelope(
            "internal_error",
            "an unexpected error occurred",
            status_code=500,
            request_id=rid,
        )
