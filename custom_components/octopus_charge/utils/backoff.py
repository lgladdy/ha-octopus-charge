"""Retry backoff helpers."""

from __future__ import annotations

from ..const import MAX_RETRY_SECONDS


def retry_after_seconds(consecutive_failures: int, base_seconds: float) -> float:
    """Return how long to wait before the next attempt after repeated failures.

    Adds triangular-number minutes (1, 3, 6, 10, ...) to the normal interval for
    each consecutive failure, capped so we never go quiet for too long.
    """
    if consecutive_failures < 1:
        return base_seconds

    extra_minutes = consecutive_failures * (consecutive_failures + 1) / 2
    return min(base_seconds + extra_minutes * 60, MAX_RETRY_SECONDS)
