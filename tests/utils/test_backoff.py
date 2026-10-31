"""Tests for retry backoff."""

from __future__ import annotations

import pytest

from custom_components.octopus_charge.const import MAX_RETRY_SECONDS
from custom_components.octopus_charge.utils.backoff import retry_after_seconds


def test_no_failures_uses_base_interval() -> None:
    """With no failures the normal interval applies."""
    assert retry_after_seconds(0, 60) == 60


@pytest.mark.parametrize(
    ("failures", "expected"),
    [(1, 60 + 60), (2, 60 + 180), (3, 60 + 360), (4, 60 + 600)],
)
def test_adds_triangular_minutes_per_failure(failures: int, expected: float) -> None:
    """Each consecutive failure adds 1, 3, 6, 10... minutes."""
    assert retry_after_seconds(failures, 60) == expected


def test_is_capped() -> None:
    """Many failures never exceed the maximum retry interval."""
    assert retry_after_seconds(1000, 60) == MAX_RETRY_SECONDS
