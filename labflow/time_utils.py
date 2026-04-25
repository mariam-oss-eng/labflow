"""Timezone-aware datetime utilities.

Centralized so we never accidentally use ``datetime.utcnow()`` (which is
deprecated in Python 3.12+) or naive datetimes anywhere in the codebase.
All persisted timestamps are UTC-aware.
"""
from __future__ import annotations

from datetime import datetime, timezone


def now_utc() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime | None) -> datetime | None:
    """Return ``dt`` coerced to UTC. Naive input is assumed to be UTC.

    SQLite stores datetimes without timezone info, so values loaded from
    the DB come back naive. This helper makes downstream code timezone-safe
    without rewriting every read site.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
