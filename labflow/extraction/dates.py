"""Date and deadline parsing for transcript text.

Designed to be deterministic given a `reference` date so tests and digests
produce stable results regardless of when the code runs.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Optional

from dateutil import parser as _du_parser

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_RELATIVE_PATTERNS = [
    (re.compile(r"\beod\b", re.I), "eod"),
    (re.compile(r"\btoday\b", re.I), "today"),
    (re.compile(r"\btomorrow\b", re.I), "tomorrow"),
    (re.compile(r"\bend of (?:the )?week\b", re.I), "eow"),
    (re.compile(r"\bend of (?:the )?month\b", re.I), "eom"),
    (re.compile(r"\bnext week\b", re.I), "next_week"),
    (re.compile(r"\bnext month\b", re.I), "next_month"),
    (re.compile(r"\bin (\d+) days?\b", re.I), "in_days"),
    (re.compile(r"\bin (\d+) weeks?\b", re.I), "in_weeks"),
    (re.compile(r"\bby (monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I),
     "by_weekday"),
    (re.compile(r"\bnext (monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I),
     "next_weekday"),
]

# Absolute date forms we want to recognize (kept conservative on purpose).
_ABSOLUTE_PATTERNS = [
    re.compile(r"\bby\s+(\d{4}-\d{2}-\d{2})\b", re.I),
    re.compile(r"\bon\s+(\d{4}-\d{2}-\d{2})\b", re.I),
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
    # "by Mar 5" / "by March 5, 2026"
    re.compile(
        r"\bby\s+("
        r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+\d{1,2}"
        r"(?:,?\s*\d{4})?"
        r")\b",
        re.I,
    ),
]


def _eod(d: date) -> datetime:
    return datetime.combine(d, time(17, 0))


def _next_weekday(reference: date, target_weekday: int) -> date:
    """Strictly *next* occurrence (always in the future)."""
    days = (target_weekday - reference.weekday()) % 7
    if days == 0:
        days = 7
    return reference + timedelta(days=days)


def _by_weekday(reference: date, target_weekday: int) -> date:
    """Within the current week if possible, otherwise the next occurrence."""
    days = (target_weekday - reference.weekday()) % 7
    if days == 0:
        days = 7
    return reference + timedelta(days=days)


def parse_due_date(text: str, reference: Optional[datetime] = None) -> Optional[datetime]:
    """Parse a deadline from free text.

    Returns ``None`` if no deadline expression is found. End-of-day defaults
    to 17:00 local time. Relative phrases ("by Friday", "next week") are
    resolved against ``reference`` (defaults to ``datetime.utcnow``).
    """
    if not text:
        return None
    ref_dt = reference or datetime.utcnow()
    ref = ref_dt.date()

    # Try absolute forms first — they are the most precise.
    for pat in _ABSOLUTE_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                parsed = _du_parser.parse(m.group(1), default=ref_dt, fuzzy=False)
            except (ValueError, OverflowError):
                continue
            # If only date provided, default to EOD.
            if parsed.hour == ref_dt.hour and parsed.minute == ref_dt.minute and parsed.second == ref_dt.second:
                parsed = _eod(parsed.date())
            return parsed

    for pat, kind in _RELATIVE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        if kind == "eod" or kind == "today":
            return _eod(ref)
        if kind == "tomorrow":
            return _eod(ref + timedelta(days=1))
        if kind == "eow":
            # End of work week: Friday EOD
            return _eod(_by_weekday(ref, 4))
        if kind == "eom":
            # First of next month minus one day
            if ref.month == 12:
                first_next = date(ref.year + 1, 1, 1)
            else:
                first_next = date(ref.year, ref.month + 1, 1)
            return _eod(first_next - timedelta(days=1))
        if kind == "next_week":
            # Monday of next week, EOD
            monday = _next_weekday(ref, 0)
            return _eod(monday)
        if kind == "next_month":
            if ref.month == 12:
                return _eod(date(ref.year + 1, 1, 1))
            return _eod(date(ref.year, ref.month + 1, 1))
        if kind == "in_days":
            return _eod(ref + timedelta(days=int(m.group(1))))
        if kind == "in_weeks":
            return _eod(ref + timedelta(weeks=int(m.group(1))))
        if kind == "by_weekday":
            return _eod(_by_weekday(ref, WEEKDAYS[m.group(1).lower()]))
        if kind == "next_weekday":
            return _eod(_next_weekday(ref, WEEKDAYS[m.group(1).lower()]))

    return None
