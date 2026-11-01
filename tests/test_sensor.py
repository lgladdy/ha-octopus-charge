"""Tests for the sensors."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.octopus_charge.api.exceptions import (
    OctopusChargeConnectionError,
)
from custom_components.octopus_charge.api.models import (
    ChargePointDaySchedule,
    ChargePointPowerReading,
)
from custom_components.octopus_charge.const import DOMAIN, OUTAGE_TOLERANCE

from .conftest import DEVICE_UUID, charge_point, update_charge_point

OPERATIONAL_STATE = "sensor.octopus_charge_operational_state"
CHARGING_METHOD = "sensor.octopus_charge_charging_method"
BOOST_END = "sensor.octopus_charge_boost_end_time"
LIVE_POWER = "sensor.octopus_charge_live_power"
ENERGY = "sensor.octopus_charge_energy"
SCHEDULE = "sensor.octopus_charge_schedule"


REAL_SLEEP = asyncio.sleep


async def _yield() -> None:
    # Uses the real sleep: some tests replace asyncio.sleep to skip backoff.
    for _ in range(10):
        await REAL_SLEEP(0)


# --- enum sensors -----------------------------------------------------------


async def test_status_sensors(hass: HomeAssistant, setup_integration: None) -> None:
    assert hass.states.get(OPERATIONAL_STATE).state == "NOT_CHARGING"
    assert hass.states.get(CHARGING_METHOD).state == "SCHEDULED"


async def test_status_follows_the_charger(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    await update_charge_point(
        hass,
        setup_integration,
        mock_client,
        operationalState="UNPLUGGED",
        chargingMethod="ON_DEMAND",
    )

    assert hass.states.get(OPERATIONAL_STATE).state == "UNPLUGGED"
    assert hass.states.get(CHARGING_METHOD).state == "ON_DEMAND"


async def test_unrecognised_value_keeps_the_last_state_and_warns_once(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)

    for _ in range(3):
        await update_charge_point(
            hass, setup_integration, mock_client, operationalState="TELEPORTING"
        )

    assert hass.states.get(OPERATIONAL_STATE).state == "NOT_CHARGING"
    assert caplog.text.count("unrecognized operational_state 'TELEPORTING'") == 1


async def test_missing_value_keeps_the_last_state(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: AsyncMock
) -> None:
    await update_charge_point(
        hass, setup_integration, mock_client, operationalState=None
    )

    assert hass.states.get(OPERATIONAL_STATE).state == "NOT_CHARGING"


# --- boost end time ---------------------------------------------------------


async def test_boost_end_time(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: AsyncMock
) -> None:
    assert hass.states.get(BOOST_END).state == STATE_UNKNOWN

    await update_charge_point(
        hass,
        setup_integration,
        mock_client,
        boostEndTime="2026-09-01T10:30:00+00:00",
    )
    assert hass.states.get(BOOST_END).state == "2026-09-01T10:30:00+00:00"

    await update_charge_point(
        hass, setup_integration, mock_client, boostEndTime="not a date"
    )
    assert hass.states.get(BOOST_END).state == STATE_UNKNOWN


async def test_boost_end_time_without_an_offset_is_utc(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: AsyncMock
) -> None:
    await update_charge_point(
        hass, setup_integration, mock_client, boostEndTime="2026-09-01T10:30:00"
    )

    assert hass.states.get(BOOST_END).state == "2026-09-01T10:30:00+00:00"


# --- schedule ---------------------------------------------------------------


async def test_schedule_sensor(hass: HomeAssistant, setup_integration: None) -> None:
    state = hass.states.get(SCHEDULE)

    assert state.state == "1 period"
    assert state.attributes["schedule"]["MONDAY"] == [
        {"start": "18:00", "end": "19:00", "action": "ON"}
    ]
    assert state.attributes["schedule"]["TUESDAY"] == []


async def test_schedule_sensor_updates_with_the_schedule(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: AsyncMock
) -> None:
    mock_client.async_get_schedules.return_value = [
        ChargePointDaySchedule.model_validate(
            {
                "day": day,
                "chargePointScheduleSettings": [
                    {"start": "01:00", "end": "05:00", "action": "ON"}
                ],
            }
        )
        for day in ("MONDAY", "TUESDAY")
    ]

    await setup_integration.runtime_data.schedule_coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(SCHEDULE).state == "2 periods"


async def test_schedule_sensor_unavailable_without_a_schedule(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    mock_client.async_get_schedules.return_value = None
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(SCHEDULE).state == STATE_UNAVAILABLE


# --- live power -------------------------------------------------------------


END = Exception("end of stream")


class FakeStream:
    """A controllable stand-in for the live power stream."""

    def __init__(self) -> None:
        self.readings: asyncio.Queue[ChargePointPowerReading | Exception | None] = (
            asyncio.Queue()
        )
        self.connections = 0

    async def __call__(
        self, account_number: str, device_uuid: str
    ) -> AsyncIterator[ChargePointPowerReading | None]:
        self.connections += 1
        while True:
            item = await self.readings.get()
            if item is END:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    def end(self) -> None:
        """End the current stream cleanly, as the server closing it would."""
        self.readings.put_nowait(END)

    def push(self, kw: float | None) -> None:
        self.readings.put_nowait(
            None if kw is None else ChargePointPowerReading(value=kw, unit="KILOWATT")
        )


@pytest.fixture
def stream(mock_client: AsyncMock) -> FakeStream:
    fake = FakeStream()
    mock_client.async_stream_power.side_effect = fake
    return fake


async def test_live_power_is_zero_when_not_charging(
    hass: HomeAssistant, setup_integration: None, stream: FakeStream
) -> None:
    assert hass.states.get(LIVE_POWER).state == "0"
    assert stream.connections == 0


async def test_live_power_streams_only_while_charging(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: AsyncMock,
    stream: FakeStream,
) -> None:
    await update_charge_point(
        hass, setup_integration, mock_client, operationalState="CHARGING"
    )
    stream.push(7.4)
    await _yield()
    await hass.async_block_till_done()

    assert stream.connections == 1
    assert hass.states.get(LIVE_POWER).state == "7.4"

    # A further update while still charging doesn't open a second stream.
    await update_charge_point(
        hass, setup_integration, mock_client, operationalState="CHARGING"
    )
    assert stream.connections == 1

    await update_charge_point(
        hass, setup_integration, mock_client, operationalState="NOT_CHARGING"
    )
    assert hass.states.get(LIVE_POWER).state == "0"


async def test_live_power_streams_straight_away_if_already_charging(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    stream: FakeStream,
) -> None:
    mock_client.async_get_charge_points.return_value = [
        charge_point(operationalState="BOOST_CHARGING")
    ]
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    stream.push(3.3)
    await _yield()
    await hass.async_block_till_done()

    assert hass.states.get(LIVE_POWER).state == "3.3"


async def test_a_missing_reading_means_zero(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: AsyncMock,
    stream: FakeStream,
) -> None:
    await update_charge_point(
        hass, setup_integration, mock_client, operationalState="CHARGING"
    )
    stream.push(7.4)
    stream.push(None)
    await _yield()
    await hass.async_block_till_done()

    assert hass.states.get(LIVE_POWER).state == "0"


async def test_dropped_stream_reconnects_with_backoff(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: AsyncMock,
    stream: FakeStream,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []

    async def fake_sleep(delay: float, *_: object) -> None:
        if delay > 0:  # ignore other code merely yielding to the loop
            delays.append(delay)
        await REAL_SLEEP(0)

    monkeypatch.setattr(
        "custom_components.octopus_charge.sensor.asyncio.sleep", fake_sleep
    )
    await update_charge_point(
        hass, setup_integration, mock_client, operationalState="CHARGING"
    )

    stream.readings.put_nowait(OctopusChargeConnectionError("dropped"))
    await _yield()
    stream.readings.put_nowait(OctopusChargeConnectionError("dropped again"))
    await _yield()
    stream.push(5.0)
    await _yield()
    await hass.async_block_till_done()

    assert delays[:2] == [5, 10]
    assert stream.connections == 3
    assert hass.states.get(LIVE_POWER).state == "5.0"


async def test_unexpected_stream_errors_are_logged_and_retried(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: AsyncMock,
    stream: FakeStream,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_sleep(delay: float, *_: object) -> None:
        await REAL_SLEEP(0)

    monkeypatch.setattr(
        "custom_components.octopus_charge.sensor.asyncio.sleep", fake_sleep
    )
    await update_charge_point(
        hass, setup_integration, mock_client, operationalState="CHARGING"
    )

    stream.readings.put_nowait(RuntimeError("boom"))
    await _yield()
    stream.push(2.0)
    await _yield()
    await hass.async_block_till_done()

    assert "Unexpected error in live power stream" in caplog.text
    assert hass.states.get(LIVE_POWER).state == "2.0"


async def test_unloading_stops_the_stream(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: AsyncMock,
    stream: FakeStream,
) -> None:
    await update_charge_point(
        hass, setup_integration, mock_client, operationalState="CHARGING"
    )
    await _yield()

    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()

    assert setup_integration.state is ConfigEntryState.NOT_LOADED


# --- energy -----------------------------------------------------------------


async def _push(hass: HomeAssistant, stream: FakeStream, kw: float | None) -> None:
    stream.push(kw)
    await _yield()
    await hass.async_block_till_done()


async def _set_state(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock, state: str
) -> None:
    await update_charge_point(hass, entry, client, operationalState=state)
    await _yield()
    await hass.async_block_till_done()


def _energy(hass: HomeAssistant) -> float:
    return float(hass.states.get(ENERGY).state)


async def test_energy_starts_at_zero(
    hass: HomeAssistant, setup_integration: None
) -> None:
    assert _energy(hass) == 0


async def test_energy_integrates_live_power(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: AsyncMock,
    stream: FakeStream,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _set_state(hass, setup_integration, mock_client, "CHARGING")
    await _push(hass, stream, 7.2)
    freezer.tick(timedelta(seconds=36))  # 0.01 hours
    await _push(hass, stream, 3.0)

    # The elapsed window is credited at the *previous* reading's power.
    assert _energy(hass) == pytest.approx(0.072)

    freezer.tick(timedelta(seconds=36))
    await _push(hass, stream, None)

    assert _energy(hass) == pytest.approx(0.072 + 0.03)


async def test_energy_counts_steady_readings(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: AsyncMock,
    stream: FakeStream,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Every reading counts, even one identical to the last."""
    await _set_state(hass, setup_integration, mock_client, "CHARGING")
    for _ in range(4):
        await _push(hass, stream, 7.2)
        freezer.tick(timedelta(seconds=10))
    await _push(hass, stream, 7.2)

    assert _energy(hass) == pytest.approx(0.08)


async def test_energy_stops_when_charging_stops_after_the_stream_ended(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: AsyncMock,
    stream: FakeStream,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A stream that ended by itself doesn't leave live power stuck on."""
    await _set_state(hass, setup_integration, mock_client, "CHARGING")
    await _push(hass, stream, 7.2)
    stream.end()
    await _yield()
    freezer.tick(timedelta(seconds=36))
    await _set_state(hass, setup_integration, mock_client, "NOT_CHARGING")

    assert hass.states.get(LIVE_POWER).state == "0"
    assert _energy(hass) == pytest.approx(0.072)

    # Polls while idle add nothing.
    for _ in range(10):
        freezer.tick(timedelta(minutes=1))
        await _set_state(hass, setup_integration, mock_client, "NOT_CHARGING")
    assert _energy(hass) == pytest.approx(0.072)


async def test_energy_does_not_count_polls_as_readings(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: AsyncMock,
    stream: FakeStream,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Polls re-render live power; a stalled stream mustn't be credited forever."""
    await _set_state(hass, setup_integration, mock_client, "CHARGING")
    await _push(hass, stream, 7.0)
    for _ in range(30):
        freezer.tick(timedelta(minutes=1))
        await _set_state(hass, setup_integration, mock_client, "CHARGING")
    await _push(hass, stream, 7.1)

    # The 30 minute gap between readings is too long to trust.
    assert _energy(hass) == 0


async def test_energy_is_independent_of_the_live_power_display_unit(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: AsyncMock,
    stream: FakeStream,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    entity_registry.async_update_entity_options(
        LIVE_POWER, "sensor", {"unit_of_measurement": "W"}
    )
    await hass.async_block_till_done()
    await _set_state(hass, setup_integration, mock_client, "CHARGING")
    await _push(hass, stream, 7.2)
    freezer.tick(timedelta(seconds=36))
    await _push(hass, stream, 7.2)

    assert float(hass.states.get(LIVE_POWER).state) == pytest.approx(7200)
    assert _energy(hass) == pytest.approx(0.072)


async def test_energy_counts_with_live_power_disabled(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    stream: FakeStream,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
) -> None:
    entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{DEVICE_UUID}_live_power",
        suggested_object_id="octopus_charge_live_power",
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    mock_client.async_get_charge_points.return_value = [
        charge_point(operationalState="CHARGING")
    ]
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    await _push(hass, stream, 7.2)
    freezer.tick(timedelta(seconds=36))
    await _push(hass, stream, 7.2)

    assert hass.states.get(LIVE_POWER) is None
    assert _energy(hass) == pytest.approx(0.072)


async def test_live_power_keeps_streaming_through_a_failed_poll(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: AsyncMock,
    stream: FakeStream,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _set_state(hass, setup_integration, mock_client, "CHARGING")

    # An outage long enough for the status entities to give up.
    mock_client.async_get_charge_points.side_effect = OctopusChargeConnectionError
    coordinator = setup_integration.runtime_data.coordinator
    await coordinator.async_refresh()
    freezer.tick(OUTAGE_TOLERANCE)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(OPERATIONAL_STATE).state == STATE_UNAVAILABLE

    await _push(hass, stream, 7.2)
    freezer.tick(timedelta(seconds=36))
    await _push(hass, stream, 7.0)

    assert hass.states.get(LIVE_POWER).state == "7.0"
    assert _energy(hass) == pytest.approx(0.072)


async def test_energy_total_is_restored(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(ENERGY, "37.25"),
                {"native_value": 37.25, "native_unit_of_measurement": "kWh"},
            )
        ],
    )
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(ENERGY).state == "37.25"


async def test_total_saved_while_unavailable_is_restored(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    """E.g. reloading after a reauth, or restarting during an outage."""
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(ENERGY, STATE_UNAVAILABLE),
                {"native_value": 5.0, "native_unit_of_measurement": "kWh"},
            )
        ],
    )
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert _energy(hass) == 5.0


async def test_entities_are_unavailable_when_the_charge_point_disappears(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    mock_client.async_get_charge_points.return_value = []
    await setup_integration.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(OPERATIONAL_STATE).state == STATE_UNAVAILABLE
