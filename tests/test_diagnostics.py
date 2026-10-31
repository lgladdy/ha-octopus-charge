"""Tests for diagnostics."""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.octopus_charge.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import ACCOUNT_NUMBER, API_KEY, DEVICE_UUID


async def test_diagnostics_redact_identifying_data(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    diagnostics = await async_get_config_entry_diagnostics(hass, setup_integration)
    dumped = json.dumps(diagnostics, default=str)

    for secret in (
        API_KEY,
        ACCOUNT_NUMBER,
        DEVICE_UUID,
        "SN123456789",
        "123456",  # BLE pin
        "8944000000000000000",  # SIM
        "external-device-1",
    ):
        assert secret not in dumped, secret


async def test_diagnostics_include_the_useful_data(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    diagnostics = await async_get_config_entry_diagnostics(hass, setup_integration)

    assert diagnostics["last_update_success"] is True
    assert diagnostics["charge_points"][0]["operationalState"] == "NOT_CHARGING"
    assert diagnostics["charge_points"][0]["configuration"]["isEcoModeEnabled"] is True
    assert diagnostics["schedules"][0]["MONDAY"] == [
        {"start": "18:00", "end": "19:00", "action": "ON"}
    ]
