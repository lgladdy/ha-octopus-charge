"""Selects for the Octopus Charge integration."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api.client import CONTROL_MODES
from .coordinator import OctopusChargeConfigEntry, OctopusChargeCoordinator
from .entity import (
    OctopusChargeEntity,
    OptimisticValue,
    async_add_charge_point_entities,
)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OctopusChargeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the selects."""
    coordinator = entry.runtime_data.coordinator
    async_add_charge_point_entities(
        entry,
        async_add_entities,
        lambda device_uuid: [OctopusChargeControlModeSelect(coordinator, device_uuid)],
    )


class OctopusChargeControlModeSelect(OctopusChargeEntity, SelectEntity):
    """Whether the charger is controlled automatically (smart) or manually."""

    def __init__(self, coordinator: OctopusChargeCoordinator, device_uuid: str) -> None:
        """Initialise the select."""
        super().__init__(coordinator, device_uuid, "control_mode")
        self._attr_options = list(CONTROL_MODES)
        self._optimistic = OptimisticValue[str]()

    @property
    def current_option(self) -> str | None:
        """Return the current control mode."""
        charge_point = self.charge_point
        fetched = charge_point.controlMode if charge_point is not None else None
        return self._optimistic.resolve(fetched, self.coordinator.last_retrieved)

    async def async_select_option(self, option: str) -> None:
        """Set the control mode."""
        await self.coordinator.client.async_set_control_mode(
            self.coordinator.account_number, self._device_uuid, option
        )
        self._optimistic.set(option)
        self.async_write_ha_state()
