"""Structured logging setup.

Uses stdlib ``logging`` (no extra deps). Two formatters are available:

  * ``KeyValueFormatter`` — human-readable, default for development
  * ``JsonFormatter`` — newline-delimited JSON, default for production
    (set via ``LABFLOW_LOG_JSON=true``)

A request-ID middleware (see :mod:`labflow.main`) attaches an ID to each
incoming request and to every log record produced while handling it.
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

# Context variable carrying the current request id, if any.
_request_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "labflow_request_id", default=None
)


def get_request_id() -> str | None:
    return _request_id_ctx.get()


def set_request_id(value: str | None) -> contextvars.Token:
    return _request_id_ctx.set(value)


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """Newline-delimited JSON. Suitable for log shippers / Loki / Datadog."""

    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "asctime",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
        }
        # Surface any extra fields passed via logger.info(..., extra={...}).
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key in payload or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=False)


class KeyValueFormatter(logging.Formatter):
    """Human-readable single-line format with key=value extras."""

    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()} "
            f"{record.levelname:<5} [{getattr(record, 'request_id', '-')}] "
            f"{record.name}: {record.getMessage()}"
        )
        extras = []
        for key, value in record.__dict__.items():
            if key in JsonFormatter._RESERVED or key in {"request_id"} or key.startswith("_"):
                continue
            extras.append(f"{key}={value!r}")
        if extras:
            base += " " + " ".join(extras)
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging(level: str = "INFO", *, json_logs: bool = False) -> None:
    """Configure the root logger. Idempotent."""
    root = logging.getLogger()
    # Remove any existing handlers we previously attached so calling this
    # again (e.g. in tests) doesn't double-format records.
    for h in list(root.handlers):
        if getattr(h, "_labflow_owned", False):
            root.removeHandler(h)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(JsonFormatter() if json_logs else KeyValueFormatter())
    handler.addFilter(_RequestIdFilter())
    handler._labflow_owned = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)
    # Quiet down chatty loggers a touch in development.
    logging.getLogger("uvicorn.access").setLevel(level)
