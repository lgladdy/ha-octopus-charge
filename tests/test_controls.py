"""Tests for the switches, select, number and binary sensor."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components import number, select, switch
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceNotSupported,
    ServiceValidationError,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.octopus_charge.api.exceptions import (
    OctopusChargeAuthError,
    OctopusChargeServerError,
)
from custom_components.octopus_charge.api.models import OnboardedChargePoint
from custom_components.octopus_charge.const import DOMAIN, OPTIMISTIC_TIMEOUT
from custom_components.octopus_charge.coordinator import OctopusChargeCoordinator

from .conftest import (
    ACCOUNT_NUMBER,
    DEVICE_UUID,
    charge_point_data,
    update_charge_point,
)

ECO_MODE = "switch.octopus_charge_eco_mode"
RANDOM_DELAY = "switch.octopus_charge_random_delay"
AWAY_MODE = "switch.octopus_charge_away_mode"
CABLE_LOCK = "switch.octopus_charge_cable_auto_lock"
BOOST = "switch.octopus_charge_boost"
CONTROL_MODE = "select.octopus_charge_control_mode"
LED = "number.octopus_charge_led_brightness"
CONNECTED = "binary_sensor.octopus_charge_connected"


@pytest.fixture
def burst() -> AsyncMock:
    """Stop real burst tasks being started by commands."""
    with patch.object(
        OctopusChargeCoordinator, "async_start_burst_refresh"
    ) as start_burst:
        yield start_burst


async def _switch(hass: HomeAssistant, service: str, entity_id: str) -> None:
    await hass.services.async_call(
        switch.DOMAIN, service, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


# --- binary sensor ----------------------------------------------------------


async def test_connected(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: AsyncMock
) -> None:
    assert hass.states.get(CONNECTED).state == STATE_ON

    config = {"isConnected": False}
    await update_charge_point(
        hass, setup_integration, mock_client, configuration=config
    )
    assert hass.states.get(CONNECTED).state == STATE_OFF

    await update_charge_point(hass, setup_integration, mock_client, configuration=None)
    assert hass.states.get(CONNECTED).state == STATE_UNKNOWN


# --- setting switches -------------------------------------------------------


async def test_setting_switches_reflect_configuration(
    hass: HomeAssistant, setup_integration: None
) -> None:
    assert hass.states.get(ECO_MODE).state == STATE_ON
    assert hass.states.get(RANDOM_DELAY).state == STATE_ON
    assert hass.states.get(AWAY_MODE).state == STATE_OFF
    assert hass.states.get(CABLE_LOCK).state == STATE_OFF


async def test_cable_lock_only_exists_when_supported(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    data = charge_point_data()
    data["configuration"]["isChargeCableAutoLockAvailable"] = False
    mock_client.async_get_charge_points.return_value = [
        OnboardedChargePoint.model_validate(data)
    ]
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(CABLE_LOCK) is None
    assert hass.states.get(ECO_MODE) is not None


async def test_cable_lock_appears_once_supported(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """The configuration can come back empty at first (e.g. charger offline)."""
    mock_client.async_get_charge_points.return_value = [
        OnboardedChargePoint.model_validate(charge_point_data(configuration=None))
    ]
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(CABLE_LOCK) is None

    await update_charge_point(hass, mock_config_entry, mock_client)

    assert hass.states.get(CABLE_LOCK).state == STATE_OFF


@pytest.mark.parametrize(
    ("entity_id", "method", "service", "expected"),
    [
        (ECO_MODE, "async_set_eco_mode", SERVICE_TURN_OFF, False),
        (RANDOM_DELAY, "async_set_random_delay", SERVICE_TURN_OFF, False),
        (AWAY_MODE, "async_set_away_mode", SERVICE_TURN_ON, True),
        (CABLE_LOCK, "async_set_cable_auto_lock", SERVICE_TURN_ON, True),
    ],
)
async def test_setting_switches_send_the_change(
    hass: HomeAssistant,
    setup_integration: None,
    mock_client: AsyncMock,
    entity_id: str,
    method: str,
    service: str,
    expected: bool,
) -> None:
    await _switch(hass, service, entity_id)

    getattr(mock_client, method).assert_awaited_once_with(
        ACCOUNT_NUMBER, DEVICE_UUID, expected
    )
    assert hass.states.get(entity_id).state == (STATE_ON if expected else STATE_OFF)


async def test_a_command_is_trusted_until_octopus_reflects_it(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _switch(hass, SERVICE_TURN_OFF, ECO_MODE)

    # Polls that still show the old value - Octopus being slow to reflect the
    # change, or a poll already in flight when the command was sent - don't
    # flip the switch back...
    freezer.tick(timedelta(seconds=30))
    await update_charge_point(hass, setup_integration, mock_client)
    assert hass.states.get(ECO_MODE).state == STATE_OFF

    # ...once Octopus reports the new value it is trusted from then on...
    config = {**charge_point_data()["configuration"], "isEcoModeEnabled": False}
    await update_charge_point(
        hass, setup_integration, mock_client, configuration=config
    )
    assert hass.states.get(ECO_MODE).state == STATE_OFF

    # ...so a later change made elsewhere shows straight away.
    await update_charge_point(hass, setup_integration, mock_client)
    assert hass.states.get(ECO_MODE).state == STATE_ON


async def test_a_command_that_never_sticks_gives_way(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _switch(hass, SERVICE_TURN_OFF, ECO_MODE)

    freezer.tick(OPTIMISTIC_TIMEOUT + timedelta(seconds=1))
    await update_charge_point(hass, setup_integration, mock_client)

    assert hass.states.get(ECO_MODE).state == STATE_ON


async def test_failed_command_is_reported_and_not_assumed(
    hass: HomeAssistant, setup_integration: None, mock_client: AsyncMock
) -> None:
    mock_client.async_set_eco_mode.side_effect = OctopusChargeServerError("down")

    with pytest.raises(HomeAssistantError):
        await _switch(hass, SERVICE_TURN_OFF, ECO_MODE)

    assert hass.states.get(ECO_MODE).state == STATE_ON


async def test_rejected_api_key_on_a_command_starts_reauth(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: AsyncMock
) -> None:
    mock_client.async_set_eco_mode.side_effect = OctopusChargeAuthError("no")

    with pytest.raises(HomeAssistantError):
        await _switch(hass, SERVICE_TURN_OFF, ECO_MODE)
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_boost_state_follows_operational_state(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: AsyncMock
) -> None:
    assert hass.states.get(BOOST).state == STATE_OFF

    await update_charge_point(
        hass, setup_integration, mock_client, operationalState="BOOST_CHARGING"
    )
    assert hass.states.get(BOOST).state == STATE_ON

    await update_charge_point(
        hass, setup_integration, mock_client, operationalState="CHARGING"
    )
    assert hass.states.get(BOOST).state == STATE_OFF


async def test_boost_on_defaults_to_an_hour_and_polls_quickly(
    hass: HomeAssistant,
    setup_integration: None,
    mock_client: AsyncMock,
    burst: AsyncMock,
) -> None:
    await _switch(hass, SERVICE_TURN_ON, BOOST)

    mock_client.async_start_boost.assert_awaited_once_with(
        ACCOUNT_NUMBER, DEVICE_UUID, dt_util.utcnow() + timedelta(hours=1)
    )
    assert hass.states.get(BOOST).state == STATE_ON
    burst.assert_called_once()


async def test_boost_off_stops_the_boost(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: AsyncMock,
    burst: AsyncMock,
) -> None:
    await update_charge_point(
        hass, setup_integration, mock_client, operationalState="BOOST_CHARGING"
    )

    await _switch(hass, SERVICE_TURN_OFF, BOOST)

    mock_client.async_stop_boost.assert_awaited_once_with(ACCOUNT_NUMBER, DEVICE_UUID)
    assert hass.states.get(BOOST).state == STATE_OFF
    burst.assert_called_once()


async def test_boost_charge_point_service(
    hass: HomeAssistant,
    setup_integration: None,
    mock_client: AsyncMock,
    burst: AsyncMock,
) -> None:
    await hass.services.async_call(
        DOMAIN,
        "boost_charge_point",
        {ATTR_ENTITY_ID: BOOST, "hours": 2, "minutes": 30},
        blocking=True,
    )

    mock_client.async_start_boost.assert_awaited_once_with(
        ACCOUNT_NUMBER, DEVICE_UUID, dt_util.utcnow() + timedelta(hours=2, minutes=30)
    )
    assert hass.states.get(BOOST).state == STATE_ON


async def test_boost_charge_point_service_validates_input(
    hass: HomeAssistant, setup_integration: None, mock_client: AsyncMock
) -> None:
    for hours, minutes in ((-1, 0), (13, 0), (0, 60)):
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN,
                "boost_charge_point",
                {ATTR_ENTITY_ID: BOOST, "hours": hours, "minutes": minutes},
                blocking=True,
            )

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "boost_charge_point",
            {ATTR_ENTITY_ID: BOOST, "hours": 0, "minutes": 0},
            blocking=True,
        )

    mock_client.async_start_boost.assert_not_awaited()


async def test_boost_charge_point_service_for_the_device(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: AsyncMock,
    device_registry: dr.DeviceRegistry,
    burst: AsyncMock,
) -> None:
    """Targeting the charger boosts it, skipping its other switches."""
    [device] = dr.async_entries_for_config_entry(
        device_registry, setup_integration.entry_id
    )

    await hass.services.async_call(
        DOMAIN,
        "boost_charge_point",
        {"device_id": device.id, "hours": 1, "minutes": 15},
        blocking=True,
    )

    mock_client.async_start_boost.assert_awaited_once()


async def test_boost_charge_point_service_on_another_switch(
    hass: HomeAssistant, setup_integration: None, mock_client: AsyncMock
) -> None:
    with pytest.raises(ServiceNotSupported):
        await hass.services.async_call(
            DOMAIN,
            "boost_charge_point",
            {ATTR_ENTITY_ID: ECO_MODE, "hours": 1, "minutes": 0},
            blocking=True,
        )

    mock_client.async_start_boost.assert_not_awaited()


# --- control mode -----------------------------------------------------------


async def test_control_mode(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    mock_client: AsyncMock,
) -> None:
    state = hass.states.get(CONTROL_MODE)
    assert state.state == "SMART"
    assert state.attributes["options"] == ["SMART", "MANUAL"]

    await hass.services.async_call(
        select.DOMAIN,
        select.SERVICE_SELECT_OPTION,
        {ATTR_ENTITY_ID: CONTROL_MODE, select.ATTR_OPTION: "MANUAL"},
        blocking=True,
    )

    mock_client.async_set_control_mode.assert_awaited_once_with(
        ACCOUNT_NUMBER, DEVICE_UUID, "MANUAL"
    )
    assert hass.states.get(CONTROL_MODE).state == "MANUAL"


async def test_invalid_control_mode_is_rejected(
    hass: HomeAssistant, setup_integration: None, mock_client: AsyncMock
) -> None:
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            select.DOMAIN,
            select.SERVICE_SELECT_OPTION,
            {ATTR_ENTITY_ID: CONTROL_MODE, select.ATTR_OPTION: "TURBO"},
            blocking=True,
        )

    mock_client.async_set_control_mode.assert_not_awaited()


# --- LED brightness ---------------------------------------------------------


async def test_led_brightness(
    hass: HomeAssistant, setup_integration: None, mock_client: AsyncMock
) -> None:
    state = hass.states.get(LED)
    assert state.state == "80"
    assert (state.attributes["min"], state.attributes["max"]) == (0, 100)

    await hass.services.async_call(
        number.DOMAIN,
        number.SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: LED, number.ATTR_VALUE: 35},
        blocking=True,
    )

    mock_client.async_set_led_brightness.assert_awaited_once_with(
        ACCOUNT_NUMBER, DEVICE_UUID, 35
    )
    assert hass.states.get(LED).state == "35"
