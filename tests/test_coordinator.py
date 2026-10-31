"""Tests for the data update coordinators."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.octopus_charge.api.exceptions import (
    OctopusChargeAuthError,
    OctopusChargeConnectionError,
    OctopusChargeServerError,
)
from custom_components.octopus_charge.const import (
    BURST_DURATION_SECONDS,
    BURST_INTERVAL_SECONDS,
    DOMAIN,
    SCAN_INTERVAL,
)
from custom_components.octopus_charge.coordinator import (
    OctopusChargeCoordinator,
    OctopusChargeScheduleCoordinator,
)

from .conftest import PROPERTY_ID, charge_point

REAL_SLEEP = asyncio.sleep


@pytest.fixture
def coordinator(setup_integration: MockConfigEntry) -> OctopusChargeCoordinator:
    return setup_integration.runtime_data.coordinator


@pytest.fixture
def schedule_coordinator(
    setup_integration: MockConfigEntry,
) -> OctopusChargeScheduleCoordinator:
    return setup_integration.runtime_data.schedule_coordinator


@pytest.fixture
def fast_sleep(freezer: FrozenDateTimeFactory) -> Generator[list[float]]:
    """Make asyncio.sleep advance the frozen clock instead of waiting."""
    requested: list[float] = []

    async def _sleep(seconds: float, *_: Any) -> None:
        requested.append(seconds)
        freezer.tick(timedelta(seconds=seconds))
        await REAL_SLEEP(0)

    with patch("asyncio.sleep", _sleep):
        yield requested


def _retry_after(coordinator: OctopusChargeCoordinator) -> float | None:
    """Return the retry delay requested by the last failed update."""
    return getattr(coordinator.last_exception, "retry_after", None)


async def _let_tasks_run() -> None:
    for _ in range(5):
        await REAL_SLEEP(0)


# --- status coordinator -----------------------------------------------------


async def test_property_ids_are_only_fetched_once(
    hass: HomeAssistant,
    coordinator: OctopusChargeCoordinator,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    await coordinator.async_refresh()
    await coordinator.async_refresh()

    assert mock_client.async_get_property_ids.await_count == 1
    assert mock_client.async_get_charge_points.await_count == 3
    mock_client.async_get_charge_points.assert_awaited_with("A-12345678", PROPERTY_ID)


async def test_data_is_keyed_by_device(
    coordinator: OctopusChargeCoordinator,
) -> None:
    assert list(coordinator.data) == [charge_point().deviceUUID]
    assert coordinator.last_retrieved is not None


async def test_account_not_found_fails_setup(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    mock_client.async_get_property_ids.return_value = None
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR


@pytest.mark.parametrize(
    "error", [OctopusChargeServerError("x"), OctopusChargeConnectionError("x")]
)
async def test_failures_back_off_and_recovery_resets(
    coordinator: OctopusChargeCoordinator, mock_client: AsyncMock, error: Exception
) -> None:
    mock_client.async_get_charge_points.side_effect = error

    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert _retry_after(coordinator) == 120  # base 60s + 1 minute

    await coordinator.async_refresh()
    assert _retry_after(coordinator) == 240  # base 60s + 3 minutes

    mock_client.async_get_charge_points.side_effect = None
    mock_client.async_get_charge_points.return_value = [charge_point()]
    await coordinator.async_refresh()
    assert coordinator.last_update_success

    mock_client.async_get_charge_points.side_effect = error
    await coordinator.async_refresh()
    assert _retry_after(coordinator) == 120  # the count started again


async def test_data_is_kept_while_failing(
    coordinator: OctopusChargeCoordinator, mock_client: AsyncMock
) -> None:
    mock_client.async_get_charge_points.side_effect = OctopusChargeServerError("x")

    await coordinator.async_refresh()

    assert list(coordinator.data) == [charge_point().deviceUUID]


async def test_rejected_api_key_starts_reauth(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    coordinator: OctopusChargeCoordinator,
    mock_client: AsyncMock,
) -> None:
    mock_client.async_get_charge_points.side_effect = OctopusChargeAuthError("bad")

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_polls_on_the_scan_interval(
    hass: HomeAssistant,
    coordinator: OctopusChargeCoordinator,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    calls = mock_client.async_get_charge_points.await_count

    freezer.tick(SCAN_INTERVAL + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert mock_client.async_get_charge_points.await_count == calls + 1


# --- burst refresh ----------------------------------------------------------


async def test_burst_polls_once_per_interval(
    coordinator: OctopusChargeCoordinator,
    mock_client: AsyncMock,
    fast_sleep: list[float],
) -> None:
    calls = mock_client.async_get_charge_points.await_count

    coordinator.async_start_burst_refresh()
    assert await _burst_finished(coordinator)

    assert fast_sleep == [BURST_INTERVAL_SECONDS] * (
        BURST_DURATION_SECONDS // BURST_INTERVAL_SECONDS
    )
    assert (
        mock_client.async_get_charge_points.await_count
        == calls + BURST_DURATION_SECONDS // BURST_INTERVAL_SECONDS
    )


async def _burst_finished(coordinator: OctopusChargeCoordinator) -> bool:
    """Wait for the burst task by yielding (a timeout would use the frozen clock)."""
    task = coordinator._burst_task
    assert task is not None
    for _ in range(200):
        if task.done():
            break
        await REAL_SLEEP(0)
    return task.done()


async def test_repeated_triggers_inside_the_interval_are_ignored(
    coordinator: OctopusChargeCoordinator,
) -> None:
    coordinator.async_start_burst_refresh()
    first = coordinator._burst_task

    coordinator.async_start_burst_refresh()

    assert coordinator._burst_task is first


async def test_a_later_trigger_replaces_the_running_burst(
    coordinator: OctopusChargeCoordinator,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator.async_start_burst_refresh()
    first = coordinator._burst_task

    freezer.tick(timedelta(seconds=BURST_INTERVAL_SECONDS + 1))
    coordinator.async_start_burst_refresh()
    await _let_tasks_run()

    assert coordinator._burst_task is not first
    assert first.cancelled()  # type: ignore[union-attr]
    assert not coordinator._burst_task.done()  # type: ignore[union-attr]


async def test_burst_stops_when_requests_are_failing(
    coordinator: OctopusChargeCoordinator,
    mock_client: AsyncMock,
    fast_sleep: list[float],
) -> None:
    calls = mock_client.async_get_charge_points.await_count
    mock_client.async_get_charge_points.side_effect = OctopusChargeServerError("x")

    coordinator.async_start_burst_refresh()
    assert await _burst_finished(coordinator)

    # One failed poll, then the burst hands over to the normal retry schedule
    # instead of carrying on for the rest of its duration.
    assert len(fast_sleep) == 2
    assert mock_client.async_get_charge_points.await_count == calls + 1


async def test_unloading_cancels_a_running_burst(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    coordinator: OctopusChargeCoordinator,
) -> None:
    coordinator.async_start_burst_refresh()
    task = coordinator._burst_task

    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()

    assert task is not None and task.done()


# --- schedule coordinator ---------------------------------------------------


async def test_schedule_is_keyed_by_device_and_day(
    schedule_coordinator: OctopusChargeScheduleCoordinator,
) -> None:
    schedule = next(iter(schedule_coordinator.data.values()))

    assert schedule["MONDAY"] == [{"start": "18:00", "end": "19:00", "action": "ON"}]
    assert schedule["TUESDAY"] == []


async def test_schedule_failures_keep_the_previous_schedule(
    schedule_coordinator: OctopusChargeScheduleCoordinator, mock_client: AsyncMock
) -> None:
    previous = schedule_coordinator.data
    mock_client.async_get_schedules.side_effect = OctopusChargeServerError("x")

    await schedule_coordinator.async_refresh()

    assert not schedule_coordinator.last_update_success
    assert schedule_coordinator.data == previous


async def test_setup_survives_a_schedule_failure(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    mock_client.async_get_schedules.side_effect = OctopusChargeServerError("x")
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_scheduled_transition_triggers_a_burst(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    # Monday 17:00 Pacific, an hour before the 18:00 period starts.
    freezer.move_to("2026-09-01 00:00:00+00:00")
    mock_config_entry.add_to_hass(hass)
    with patch("custom_components.octopus_charge.coordinator.random.uniform") as jitter:
        jitter.return_value = 0
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        coordinator = mock_config_entry.runtime_data.coordinator
        assert coordinator._burst_task is None

        # The burst starts a few seconds *before* the boundary.
        freezer.move_to("2026-09-01 00:59:56+00:00")
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    assert coordinator._burst_task is not None


async def test_unloading_cancels_the_transition_timers(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    schedule_coordinator: OctopusChargeScheduleCoordinator,
) -> None:
    assert schedule_coordinator._cancel_timers

    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()

    assert not schedule_coordinator._cancel_timers


# --- entities follow the coordinator ---------------------------------------


async def test_new_charge_points_get_entities(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    coordinator: OctopusChargeCoordinator,
    mock_client: AsyncMock,
) -> None:
    second = charge_point(deviceUUID="99999999-1111-2222-3333-444444444444")
    mock_client.async_get_charge_points.return_value = [charge_point(), second]

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get("switch.octopus_charge_boost_2") is not None
