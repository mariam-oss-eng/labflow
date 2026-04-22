"""Date / deadline parsing tests."""
from datetime import datetime

import pytest

from labflow.extraction.dates import parse_due_date


REF = datetime(2026, 4, 22, 10, 0)  # a Wednesday


@pytest.mark.parametrize(
    "text, expected",
    [
        ("ship by 2026-05-01", datetime(2026, 5, 1, 17, 0)),
        ("on 2026-05-01 we'll review", datetime(2026, 5, 1, 17, 0)),
        ("by Mar 5, 2026", datetime(2026, 3, 5, 17, 0)),
    ],
)
def test_absolute_dates(text, expected):
    assert parse_due_date(text, reference=REF) == expected


def test_today_eod():
    assert parse_due_date("done today", reference=REF) == datetime(2026, 4, 22, 17, 0)


def test_tomorrow():
    assert parse_due_date("tomorrow please", reference=REF) == datetime(2026, 4, 23, 17, 0)


def test_eod():
    assert parse_due_date("EOD please", reference=REF) == datetime(2026, 4, 22, 17, 0)


def test_by_friday_within_week():
    # Wed -> Friday is +2 days
    assert parse_due_date("by Friday", reference=REF) == datetime(2026, 4, 24, 17, 0)


def test_by_monday_jumps_to_next_week():
    # Wed -> next Monday is +5 days
    assert parse_due_date("by Monday", reference=REF) == datetime(2026, 4, 27, 17, 0)


def test_next_monday_strictly_future():
    # Even if reference is Monday, "next Monday" is +7 days
    monday = datetime(2026, 4, 20, 9, 0)
    assert parse_due_date("next Monday", reference=monday) == datetime(2026, 4, 27, 17, 0)


def test_in_n_days():
    assert parse_due_date("in 3 days", reference=REF) == datetime(2026, 4, 25, 17, 0)


def test_in_n_weeks():
    assert parse_due_date("in 2 weeks", reference=REF) == datetime(2026, 5, 6, 17, 0)


def test_end_of_week():
    assert parse_due_date("end of week", reference=REF) == datetime(2026, 4, 24, 17, 0)


def test_end_of_month():
    assert parse_due_date("end of the month", reference=REF) == datetime(2026, 4, 30, 17, 0)


def test_no_match_returns_none():
    assert parse_due_date("no deadline mentioned here", reference=REF) is None


def test_empty_text():
    assert parse_due_date("", reference=REF) is None
