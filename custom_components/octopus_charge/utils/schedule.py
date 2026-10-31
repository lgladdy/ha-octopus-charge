"""Schedule helpers."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

# datetime.weekday() is 0=Monday..6=Sunday - matches the day names the API
# itself uses for schedule entries.
DAY_NAMES = [
    "MONDAY",
    "TUESDAY",
    "WEDNESDAY",
    "THURSDAY",
    "FRIDAY",
    "SATURDAY",
    "SUNDAY",
]


def compute_next_schedule_transition(
    schedule_by_day: dict[str, list[dict[str, Any]]], now: datetime
) -> datetime | None:
    """Find the next start or end time across the whole weekly schedule.

    Strictly after ``now``. A pure function so it can be unit tested without a
    real hass instance.

    Checks a full week ahead (not just today/tomorrow) so a schedule with
    periods on only one day of the week is still found correctly. Returns None if
    the schedule has no periods at all.
    """
    candidates: list[datetime] = []
    # Yesterday (whose overnight period can end today) through a full week
    # ahead, to catch "next week" wraparound.
    for day_offset in range(-1, 8):
        day = now + timedelta(days=day_offset)
        periods = schedule_by_day.get(DAY_NAMES[day.weekday()], [])
        for period in periods:
            start = _time_on(day, period.get("start"))
            end = _time_on(day, period.get("end"))
            # A period that ends at or before it starts (e.g. 23:00-06:00, or
            # 18:00-00:00) runs past midnight, so it ends on the next day.
            if start is not None and end is not None and end <= start:
                end += timedelta(days=1)

            candidates.extend(
                boundary for boundary in (start, end) if boundary and boundary > now
            )

    return min(candidates) if candidates else None


def _time_on(day: datetime, time_str: Any) -> datetime | None:
    """Return an "HH:MM" schedule time on the given day, or None if invalid."""
    if not time_str:
        return None
    try:
        hour, minute = (int(part) for part in time_str.split(":")[:2])
        return day.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError, AttributeError:
        return None
