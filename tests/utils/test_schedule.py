"""Tests for schedule transitions."""

from __future__ import annotations

from datetime import datetime

from custom_components.octopus_charge.utils.schedule import (
    compute_next_schedule_transition,
)

MONDAY_10AM = datetime(2026, 8, 31, 10, 0, 0)


def test_period_starting_later_today_is_next() -> None:
    """The start of a period later today is the next boundary, not its end."""
    schedule = {"MONDAY": [{"start": "18:00", "end": "19:00", "action": "ON"}]}

    assert compute_next_schedule_transition(schedule, MONDAY_10AM) == datetime(
        2026, 8, 31, 18, 0
    )


def test_inside_a_period_its_end_is_next() -> None:
    """Part-way through a period the end is the next boundary."""
    schedule = {"MONDAY": [{"start": "18:00", "end": "19:00", "action": "ON"}]}

    result = compute_next_schedule_transition(schedule, datetime(2026, 8, 31, 18, 30))

    assert result == datetime(2026, 8, 31, 19, 0)


def test_next_day_with_a_period_is_used_once_today_has_passed() -> None:
    """Falls through to the next day that has a period."""
    schedule = {
        "MONDAY": [{"start": "18:00", "end": "19:00", "action": "ON"}],
        "WEDNESDAY": [{"start": "07:00", "end": "08:00", "action": "ON"}],
    }

    result = compute_next_schedule_transition(schedule, datetime(2026, 8, 31, 20, 0))

    assert result == datetime(2026, 9, 2, 7, 0)


def test_no_periods_returns_none() -> None:
    """An empty schedule has no transitions."""
    assert compute_next_schedule_transition({}, MONDAY_10AM) is None


def test_wraps_to_next_week_when_only_one_day_scheduled() -> None:
    """A single weekly period that has passed resolves to next week."""
    schedule = {"MONDAY": [{"start": "07:00", "end": "08:00", "action": "ON"}]}

    result = compute_next_schedule_transition(schedule, datetime(2026, 8, 31, 20, 0))

    assert result == datetime(2026, 9, 7, 7, 0)


def test_unparseable_times_are_skipped() -> None:
    """Out-of-range or malformed times don't raise, the valid one is used."""
    schedule = {
        "MONDAY": [
            {"start": "25:00", "end": "garbage", "action": "ON"},
            {"start": None, "end": "19:00", "action": "ON"},
        ]
    }

    assert compute_next_schedule_transition(schedule, MONDAY_10AM) == datetime(
        2026, 8, 31, 19, 0
    )
