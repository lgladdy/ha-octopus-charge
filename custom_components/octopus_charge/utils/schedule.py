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
    # Today plus a full week, to catch "next week" wraparound.
    for day_offset in range(8):
        day = now + timedelta(days=day_offset)
        periods = schedule_by_day.get(DAY_NAMES[day.weekday()], [])
        for period in periods:
            for key in ("start", "end"):
                time_str = period.get(key)
                if not time_str:
                    continue
                try:
                    hour, minute = (int(part) for part in time_str.split(":")[:2])
                    candidate = day.replace(
                        hour=hour, minute=minute, second=0, microsecond=0
                    )
                except ValueError, AttributeError:
                    continue

                if candidate > now:
                    candidates.append(candidate)

    return min(candidates) if candidates else None
