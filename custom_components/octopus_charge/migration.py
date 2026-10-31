"""Temporary one-off migration from the fork's octopus_energy charge point entities.

Only needed by anyone who ran the charge point support from the (unmerged) fork of
the Octopus Energy integration. Hands the old entities over to this integration so
their history and the energy total survive. Remove before the first release.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import restore_state

from .const import DEVICE_NAME, DOMAIN, MANUFACTURER
from .coordinator import OctopusChargeConfigEntry

_LOGGER = logging.getLogger(__name__)

OLD_DOMAIN = "octopus_energy"
OLD_UNIQUE_ID_PREFIX = "octopus_energy_charge_point_"
# Diagnostics entity of the old integration, no longer needed.
OLD_LAST_RETRIEVED_SUFFIX = "_charge_point_data_last_retrieved"


async def async_migrate_from_octopus_energy(
    hass: HomeAssistant, entry: OctopusChargeConfigEntry
) -> None:
    """Move any old charge point entities of this account to this integration."""
    entity_registry = er.async_get(hass)
    old_entries = [
        registry_entry
        for registry_entry in entity_registry.entities.values()
        if registry_entry.platform == OLD_DOMAIN
        and registry_entry.unique_id.startswith(OLD_UNIQUE_ID_PREFIX)
    ]
    if not old_entries:
        return

    charge_points = entry.runtime_data.coordinator.data
    device_registry = dr.async_get(hass)
    restore_data = restore_state.async_get(hass)

    for old in old_entries:
        device_uuid, _, key = old.unique_id.removeprefix(
            OLD_UNIQUE_ID_PREFIX
        ).partition("_")
        charge_point = charge_points.get(device_uuid)
        if charge_point is None or not key:
            # Belongs to some other account (or one that no longer exists).
            continue

        device = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, device_uuid)},
            manufacturer=MANUFACTURER,
            model=charge_point.model,
            name=DEVICE_NAME,
        )

        old_entity_id = old.entity_id
        new_entity_id = f"{old.domain}.{DOMAIN}_{key}"
        if new_entity_id != old_entity_id and (
            entity_registry.async_get(new_entity_id) is not None
        ):
            _LOGGER.warning(
                "Not renaming %s to %s - that entity already exists",
                old_entity_id,
                new_entity_id,
            )
            new_entity_id = old_entity_id

        try:
            entity_registry.async_update_entity_platform(
                old_entity_id,
                DOMAIN,
                new_config_entry_id=entry.entry_id,
                new_unique_id=f"{device_uuid}_{key}",
                new_device_id=device.id,
            )
        except ValueError as err:
            _LOGGER.warning("Unable to migrate %s: %s", old_entity_id, err)
            continue

        # Restored state (e.g. the energy total) is keyed by entity id, so it has
        # to be moved across with the rename or the entity would start from zero.
        stored = restore_data.last_states.get(old_entity_id)
        if stored is not None and new_entity_id != old_entity_id:
            restore_data.last_states[new_entity_id] = restore_state.StoredState(
                State(new_entity_id, stored.state.state, stored.state.attributes),
                stored.extra_data,
                stored.last_seen,
            )
            del restore_data.last_states[old_entity_id]

        if new_entity_id != old_entity_id:
            entity_registry.async_update_entity(
                old_entity_id, new_entity_id=new_entity_id
            )

        _LOGGER.info("Migrated %s to %s", old_entity_id, new_entity_id)

    for leftover in [
        registry_entry
        for registry_entry in entity_registry.entities.values()
        if registry_entry.platform == OLD_DOMAIN
        and registry_entry.unique_id.startswith("octopus_energy_")
        and registry_entry.unique_id.endswith(OLD_LAST_RETRIEVED_SUFFIX)
        and registry_entry.unique_id.removeprefix("octopus_energy_").removesuffix(
            OLD_LAST_RETRIEVED_SUFFIX
        )
        in charge_points
    ]:
        entity_registry.async_remove(leftover.entity_id)
