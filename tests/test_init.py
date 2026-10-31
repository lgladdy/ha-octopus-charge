"""Tests for setup and unload of the integration."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.octopus_charge.api.exceptions import (
    OctopusChargeAuthError,
    OctopusChargeServerError,
)
from custom_components.octopus_charge.const import DOMAIN

from .conftest import DEVICE_UUID


async def test_setup_and_unload(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The entry sets up and unloads cleanly."""
    assert setup_integration.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert setup_integration.state is ConfigEntryState.NOT_LOADED


async def test_creates_device_and_entities(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """A single device is created with an entity for each feature."""
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, DEVICE_UUID), setup_integration.entry_id
    )
    assert device is not None
    assert device.manufacturer == "Octopus Energy"
    assert device.model == "Charge"
    assert device.sw_version == "1.2.3"

    entries = er.async_entries_for_config_entry(
        entity_registry, setup_integration.entry_id
    )
    assert {entry.unique_id for entry in entries} == {
        f"{DEVICE_UUID}_{key}"
        for key in (
            "operational_state",
            "charging_method",
            "boost_end_time",
            "live_power",
            "energy",
            "schedule",
            "connected",
            "eco_mode",
            "random_delay",
            "away_mode",
            "cable_auto_lock",
            "boost",
            "control_mode",
            "led_brightness",
        )
    }


async def test_setup_retries_when_octopus_is_unavailable(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """A server error on first refresh leaves the entry to retry later."""
    mock_client.async_get_property_ids.side_effect = OctopusChargeServerError("down")
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_starts_reauth_when_api_key_rejected(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    """A rejected API key starts a re-authentication flow."""
    mock_client.async_get_property_ids.side_effect = OctopusChargeAuthError("bad key")
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]
