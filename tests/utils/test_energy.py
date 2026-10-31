"""Tests for energy integration."""

from __future__ import annotations

import pytest

from custom_components.octopus_charge.utils.energy import integrate_energy_kwh


def test_constant_power_accumulates() -> None:
    """7kW for a realistic 5 second gap adds the expected energy."""
    elapsed_hours = 5 / 3600

    result = integrate_energy_kwh(10.0, 7.0, elapsed_hours)

    assert result == round(10.0 + (7.0 * elapsed_hours), 4)


def test_many_short_steps_accumulate_correctly() -> None:
    """7kW for 30 minutes in 5 second steps adds 3.5kWh."""
    elapsed_hours = 5 / 3600
    total = 10.0
    for _ in range(int(0.5 / elapsed_hours)):
        total = integrate_energy_kwh(total, 7.0, elapsed_hours)

    assert total == pytest.approx(13.5, abs=0.01)


@pytest.mark.parametrize("elapsed_hours", [0, -0.1])
def test_non_positive_elapsed_leaves_total_unchanged(elapsed_hours: float) -> None:
    """A clock that stood still or went backwards adds nothing."""
    assert integrate_energy_kwh(10.0, 7.0, elapsed_hours) == 10.0


def test_gap_over_max_step_leaves_total_unchanged() -> None:
    """A long gap (e.g. HA restart mid-charge) doesn't fabricate energy."""
    assert integrate_energy_kwh(10.0, 7.0, 1, max_step_hours=0.1) == 10.0


def test_gap_just_under_max_step_is_counted() -> None:
    """A gap inside the limit is still integrated."""
    result = integrate_energy_kwh(10.0, 7.0, 0.099, max_step_hours=0.1)

    assert result == round(10.0 + (7.0 * 0.099), 4)


def test_zero_power_leaves_total_unchanged() -> None:
    """Not charging adds no energy."""
    assert integrate_energy_kwh(10.0, 0, 0.05) == 10.0
