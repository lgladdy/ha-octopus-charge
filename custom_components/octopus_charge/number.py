"""Numbers for the Octopus Charge integration."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

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
    """Set up the numbers."""
    coordinator = entry.runtime_data.coordinator
    async_add_charge_point_entities(
        entry,
        async_add_entities,
        lambda device_uuid: [
            OctopusChargeLedBrightnessNumber(coordinator, device_uuid)
        ],
    )


class OctopusChargeLedBrightnessNumber(OctopusChargeEntity, NumberEntity):
    """The brightness of the charger's status LED."""

    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: OctopusChargeCoordinator, device_uuid: str) -> None:
        """Initialise the number."""
        super().__init__(coordinator, device_uuid, "led_brightness")
        self._optimistic = OptimisticValue[float]()

    @property
    def native_value(self) -> float | None:
        """Return the current LED brightness percentage."""
        charge_point = self.charge_point
        fetched = (
            charge_point.configuration.LEDBrightnessPercentage
            if charge_point is not None and charge_point.configuration is not None
            else None
        )
        return self._optimistic.resolve(fetched)

    async def async_set_native_value(self, value: float) -> None:
        """Set the LED brightness percentage."""
        brightness = int(value)
        await self._async_send(
            self.coordinator.client.async_set_led_brightness(
                self.coordinator.account_number, self._device_uuid, brightness
            )
        )
        self._optimistic.set(brightness)
        self.async_write_ha_state()
