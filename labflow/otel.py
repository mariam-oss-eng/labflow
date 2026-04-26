"""OpenTelemetry instrumentation (v0.7).

Graceful degradation: when ``opentelemetry-api`` is not installed the
module exposes the same API as a no-op. Apps don't have to gate every
``with tracer.start_as_current_span(...)`` block on a feature flag.

Operators turn it on by installing the SDK + an exporter and setting::

    pip install opentelemetry-sdk opentelemetry-exporter-otlp
    export LABFLOW_OTEL_ENABLED=true
    export OTEL_EXPORTER_OTLP_ENDPOINT=https://my-collector:4317

LabFlow then auto-instruments the FastAPI app, the SQLAlchemy engine,
the job worker, and the webhook deliverer with consistent span names so
downstream traces are immediately useful.
"""
from __future__ import annotations

import contextlib
import logging
import os
from typing import Any, Iterator

log = logging.getLogger("labflow.otel")

_ENABLED = False
_TRACER = None
_METER = None


def setup() -> bool:
    """Initialize tracing/metrics if OTEL is available + enabled. Idempotent."""
    global _ENABLED, _TRACER, _METER
    if _ENABLED:
        return True
    if os.environ.get("LABFLOW_OTEL_ENABLED", "").lower() not in ("1", "true", "yes"):
        return False
    try:
        from opentelemetry import metrics, trace  # type: ignore
        from opentelemetry.sdk.resources import Resource  # type: ignore
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore
        from opentelemetry.sdk.trace.export import (  # type: ignore
            BatchSpanProcessor,
            ConsoleSpanExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider  # type: ignore
    except Exception:  # noqa: BLE001
        log.warning("LABFLOW_OTEL_ENABLED set but opentelemetry not installed")
        return False
    resource = Resource.create({"service.name": "labflow",
                                "service.version": "0.7.0"})
    provider = TracerProvider(resource=resource)
    # Try OTLP first; fall back to console so dev still sees output.
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore
            OTLPSpanExporter,
        )
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    except Exception:  # noqa: BLE001
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    metrics.set_meter_provider(MeterProvider(resource=resource))
    _TRACER = trace.get_tracer("labflow")
    _METER = metrics.get_meter("labflow")
    _ENABLED = True
    log.info("OpenTelemetry enabled")
    return True


def instrument_fastapi(app: Any) -> None:
    """Best-effort FastAPI instrumentation. No-op when SDK absent."""
    if not _ENABLED:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor  # type: ignore
        FastAPIInstrumentor.instrument_app(app)
    except Exception:  # noqa: BLE001
        log.debug("fastapi instrumentation unavailable", exc_info=True)


def instrument_sqlalchemy(engine: Any) -> None:
    if not _ENABLED:
        return
    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor  # type: ignore
        SQLAlchemyInstrumentor().instrument(engine=engine)
    except Exception:  # noqa: BLE001
        log.debug("sqlalchemy instrumentation unavailable", exc_info=True)


@contextlib.contextmanager
def span(name: str, **attrs: Any) -> Iterator[Any]:
    """Context manager that opens a span when OTEL is available, else no-op."""
    if not _ENABLED or _TRACER is None:
        yield None
        return
    with _TRACER.start_as_current_span(name) as sp:
        for k, v in attrs.items():
            try:
                sp.set_attribute(k, v)
            except Exception:  # noqa: BLE001
                pass
        yield sp


def is_enabled() -> bool:
    return _ENABLED
