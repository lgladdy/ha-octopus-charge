"""Fixtures for the Octopus Charge tests."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.octopus_charge.api.models import (
    ChargePointDaySchedule,
    OnboardedChargePoint,
)
from custom_components.octopus_charge.const import CONF_ACCOUNT_NUMBER, DOMAIN

ACCOUNT_NUMBER = "A-12345678"
API_KEY = "sk_live_test"
PROPERTY_ID = "12345"
DEVICE_UUID = "00000000-1111-2222-3333-444444444444"


def charge_point_data(**overrides: Any) -> dict[str, Any]:
    """Return a charge point shaped like the API response."""
    data: dict[str, Any] = {
        "deviceUUID": DEVICE_UUID,
        "model": "Charge",
        "serialNumber": "SN123456789",
        "bluetoothLowEnergyPin": "123456",
        "simcardIdentifier": "8944000000000000000",
        "firmwareVersion": "1.2.3",
        "controlMode": "SMART",
        "chargingMethod": "SCHEDULED",
        "operationalState": "NOT_CHARGING",
        "boostEndTime": None,
        "onboarding": {
            "accountNumber": ACCOUNT_NUMBER,
            "propertyId": PROPERTY_ID,
            "onboardedAt": "2024-01-01T00:00:00+00:00",
            "externalDeviceId": "external-device-1",
        },
        "configuration": {
            "isRandomDelayEnabled": True,
            "isConnected": True,
            "LEDBrightnessPercentage": 80,
            "isChargeCableAutoLockAvailable": True,
            "isChargeCableAutoLockEnabled": False,
            "isEcoModeEnabled": True,
            "isAwayMode": False,
        },
    }
    data.update(overrides)
    return data


def charge_point(**overrides: Any) -> OnboardedChargePoint:
    """Return a charge point model."""
    return OnboardedChargePoint.model_validate(charge_point_data(**overrides))


def schedules() -> list[ChargePointDaySchedule]:
    """Return a weekly schedule with a single evening period on Mondays."""
    return [
        ChargePointDaySchedule.model_validate(
            {
                "day": "MONDAY",
                "chargePointScheduleSettings": [
                    {"start": "18:00", "end": "19:00", "action": "ON"}
                ],
            }
        ),
        ChargePointDaySchedule.model_validate(
            {"day": "TUESDAY", "chargePointScheduleSettings": None}
        ),
    ]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading the custom integration in every test."""


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a config entry for a test account."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=ACCOUNT_NUMBER,
        title=ACCOUNT_NUMBER,
        data={CONF_API_KEY: API_KEY, CONF_ACCOUNT_NUMBER: ACCOUNT_NUMBER},
    )


@pytest.fixture
def mock_client() -> Generator[AsyncMock]:
    """Mock the API client everywhere it is constructed."""
    with (
        patch(
            "custom_components.octopus_charge.OctopusChargeApiClient", autospec=True
        ) as client_class,
        patch(
            "custom_components.octopus_charge.config_flow.OctopusChargeApiClient",
            new=client_class,
        ),
    ):
        client = client_class.return_value
        client.async_get_property_ids.return_value = [PROPERTY_ID]
        client.async_get_charge_points.return_value = [charge_point()]
        client.async_get_schedules.return_value = schedules()
        yield client


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> MockConfigEntry:
    """Set up the integration against a mocked client.

    Time is frozen *before* setup: freezing afterwards makes the monotonic clock
    jump, which makes every timer scheduled during setup instantly due.
    """
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry


async def update_charge_point(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock, **overrides: Any
) -> None:
    """Make the API report a changed charge point and poll it."""
    client.async_get_charge_points.return_value = [charge_point(**overrides)]
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
