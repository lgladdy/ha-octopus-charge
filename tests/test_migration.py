"""Tests for the temporary migration from the fork's octopus_energy entities."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.octopus_charge.const import DOMAIN

from .conftest import DEVICE_UUID

OTHER_UUID = "99999999-1111-2222-3333-444444444444"
OLD_ENERGY_ID = (
    "sensor.octopus_energy_charge_point_00000000_1111_2222_3333_444444444444_energy"
)


def _add_old_entity(
    registry: er.EntityRegistry,
    old_entry: MockConfigEntry,
    domain: str,
    uuid: str,
    key: str,
) -> er.RegistryEntry:
    return registry.async_get_or_create(
        domain,
        "octopus_energy",
        f"octopus_energy_charge_point_{uuid}_{key}",
        config_entry=old_entry,
        suggested_object_id=(
            f"octopus_energy_charge_point_{uuid.replace('-', '_')}_{key}"
        ),
    )


async def test_old_entities_are_handed_over(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    old_entry = MockConfigEntry(domain="octopus_energy")
    old_entry.add_to_hass(hass)
    energy = _add_old_entity(
        entity_registry, old_entry, "sensor", DEVICE_UUID, "energy"
    )
    boost = _add_old_entity(entity_registry, old_entry, "switch", DEVICE_UUID, "boost")
    last_retrieved = entity_registry.async_get_or_create(
        "sensor",
        "octopus_energy",
        f"octopus_energy_{DEVICE_UUID}_charge_point_data_last_retrieved",
        config_entry=old_entry,
    )
    unrelated = _add_old_entity(
        entity_registry, old_entry, "sensor", OTHER_UUID, "energy"
    )
    assert energy.entity_id == OLD_ENERGY_ID
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(OLD_ENERGY_ID, "127.0919"),
                {"native_value": 127.0919, "native_unit_of_measurement": "kWh"},
            )
        ],
    )
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    # The old registry entries became this integration's, with the new ids.
    migrated = entity_registry.async_get("sensor.octopus_charge_energy")
    assert migrated is not None
    assert migrated.id == energy.id  # same registry entry => history follows
    assert migrated.platform == DOMAIN
    assert migrated.unique_id == f"{DEVICE_UUID}_energy"
    assert migrated.config_entry_id == mock_config_entry.entry_id
    boosted = entity_registry.async_get("switch.octopus_charge_boost")
    assert boosted is not None and boosted.id == boost.id

    # ...attached to this integration's device.
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, DEVICE_UUID), mock_config_entry.entry_id
    )
    assert device is not None
    assert migrated.device_id == device.id

    # ...and the energy total carried over instead of restarting from zero.
    assert hass.states.get("sensor.octopus_charge_energy").state == "127.0919"

    # No duplicates and nothing left behind.
    assert entity_registry.async_get(OLD_ENERGY_ID) is None
    assert entity_registry.async_get(last_retrieved.entity_id) is None
    owned = er.async_entries_for_config_entry(
        entity_registry, mock_config_entry.entry_id
    )
    assert len({entry.unique_id for entry in owned}) == len(owned) == 14

    # Another account's entities are left alone.
    still = entity_registry.async_get(unrelated.entity_id)
    assert still is not None and still.platform == "octopus_energy"


async def test_migration_is_a_no_op_once_done(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    entity_registry: er.EntityRegistry,
) -> None:
    old_entry = MockConfigEntry(domain="octopus_energy")
    old_entry.add_to_hass(hass)
    _add_old_entity(entity_registry, old_entry, "switch", DEVICE_UUID, "boost")
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("switch.octopus_charge_boost") is not None
    assert hass.states.get("switch.octopus_charge_boost_2") is None


async def test_existing_target_entity_is_not_overwritten(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_client: AsyncMock,
    entity_registry: er.EntityRegistry,
) -> None:
    old_entry = MockConfigEntry(domain="octopus_energy")
    old_entry.add_to_hass(hass)
    old = _add_old_entity(entity_registry, old_entry, "switch", DEVICE_UUID, "boost")
    squatter = entity_registry.async_get_or_create(
        "switch", "something_else", "x", suggested_object_id="octopus_charge_boost"
    )
    mock_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert entity_registry.async_get(squatter.entity_id).platform == "something_else"
    moved = entity_registry.async_get(old.entity_id)
    assert moved is not None and moved.platform == DOMAIN
