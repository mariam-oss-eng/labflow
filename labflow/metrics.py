"""Minimal Prometheus exporter — no third-party dependency.

We expose enough to satisfy the standard "RED" trio (Rate, Errors,
Duration) for HTTP requests, plus DB-backed counters for jobs, audit
events, and webhook deliveries. Output is the Prometheus text exposition
format v0.0.4. If a team needs histograms or labels with high cardinality
they should swap in ``prometheus_client``.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models


_lock = threading.Lock()
_counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
_summaries: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = defaultdict(list)


def inc(name: str, value: float = 1.0, **labels: str) -> None:
    key = (name, tuple(sorted(labels.items())))
    with _lock:
        _counters[key] += value


def observe(name: str, value: float, **labels: str) -> None:
    """Record an observation. We track count, sum, and a small reservoir."""
    key = (name, tuple(sorted(labels.items())))
    with _lock:
        bucket = _summaries[key]
        bucket.append(value)
        if len(bucket) > 1024:
            # Cheap downsampling — keep the most recent half.
            del bucket[:512]


def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in labels) + "}"


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render(db: Session | None = None) -> str:
    """Return the full Prometheus text exposition."""
    out: list[str] = []

    with _lock:
        seen_help: set[str] = set()
        for (name, labels), value in sorted(_counters.items()):
            if name not in seen_help:
                out.append(f"# HELP {name} counter")
                out.append(f"# TYPE {name} counter")
                seen_help.add(name)
            out.append(f"{name}{_format_labels(labels)} {value}")
        for (name, labels), bucket in sorted(_summaries.items()):
            if not bucket:
                continue
            base = name
            if base not in seen_help:
                out.append(f"# HELP {base} summary")
                out.append(f"# TYPE {base} summary")
                seen_help.add(base)
            count = len(bucket)
            total = sum(bucket)
            out.append(f"{base}_count{_format_labels(labels)} {count}")
            out.append(f"{base}_sum{_format_labels(labels)} {total:.6f}")

    if db is not None:
        # DB-backed gauges — small and bounded queries only.
        for status, label in (("queued", "queued"), ("running", "running"),
                              ("failed", "failed")):
            n = db.execute(
                select(func.count())
                .select_from(models.Job)
                .where(models.Job.status == status)
            ).scalar_one()
            out.append("# HELP labflow_jobs jobs by status")
            out.append("# TYPE labflow_jobs gauge")
            out.append(f'labflow_jobs{{status="{label}"}} {n}')
        n_audit = db.execute(
            select(func.count()).select_from(models.AuditEvent)
        ).scalar_one()
        out.append("# HELP labflow_audit_events_total total audit events recorded")
        out.append("# TYPE labflow_audit_events_total counter")
        out.append(f"labflow_audit_events_total {n_audit}")

    return "\n".join(out) + "\n"
