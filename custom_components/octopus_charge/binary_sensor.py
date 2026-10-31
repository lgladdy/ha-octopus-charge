"""Binary sensors for the Octopus Charge integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import OctopusChargeConfigEntry, OctopusChargeCoordinator
from .entity import OctopusChargeEntity, async_add_charge_point_entities

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OctopusChargeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the binary sensors."""
    coordinator = entry.runtime_data.coordinator
    async_add_charge_point_entities(
        entry,
        async_add_entities,
        lambda device_uuid: [OctopusChargeConnectedSensor(coordinator, device_uuid)],
    )


class OctopusChargeConnectedSensor(OctopusChargeEntity, BinarySensorEntity):
    """Whether the charger is connected to the internet."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: OctopusChargeCoordinator, device_uuid: str) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, device_uuid, "connected")

    @property
    def is_on(self) -> bool | None:
        """Return whether the charger is connected."""
        charge_point = self.charge_point
        if charge_point is None or charge_point.configuration is None:
            return None

        return charge_point.configuration.isConnected
