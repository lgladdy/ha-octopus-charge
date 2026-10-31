"""Base entity and helpers for the Octopus Charge integration."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api.models import OnboardedChargePoint
from .const import DEVICE_NAME, DOMAIN, MANUFACTURER
from .coordinator import OctopusChargeConfigEntry, OctopusChargeCoordinator


@callback
def async_add_charge_point_entities(
    entry: OctopusChargeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: Callable[[str], Iterable[Entity]],
) -> None:
    """Add entities for every charge point, including ones that appear later."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    @callback
    def _async_add_new() -> None:
        new_devices = [
            device_uuid for device_uuid in coordinator.data if device_uuid not in known
        ]
        if not new_devices:
            return

        known.update(new_devices)
        async_add_entities(
            entity for device_uuid in new_devices for entity in factory(device_uuid)
        )

    _async_add_new()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new))


class OptimisticValue[T]:
    """Holds a value we've just sent to the charger until fresher data arrives.

    After a command, Octopus can take a little while to reflect the change, so an
    immediate poll can read the old value back and make the entity flip-flop. For
    as long as the latest retrieved data pre-dates the command, trust the
    command; once newer data has been retrieved, it wins.
    """

    def __init__(self) -> None:
        """Initialise with nothing pending."""
        self._value: T | None = None
        self._set_at: datetime | None = None

    def set(self, value: T) -> None:
        """Record a value that has just been sent to the charger."""
        self._value = value
        self._set_at = dt_util.utcnow()

    def resolve(self, fetched: T | None, last_retrieved: datetime | None) -> T | None:
        """Return the value the entity should currently show."""
        if self._set_at is not None:
            if last_retrieved is None or last_retrieved <= self._set_at:
                return self._value

            self._set_at = None
            self._value = None

        return fetched


class OctopusChargeEntity(CoordinatorEntity[OctopusChargeCoordinator]):
    """Common base for entities that represent part of a charge point."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OctopusChargeCoordinator,
        device_uuid: str,
        key: str,
    ) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self._device_uuid = device_uuid
        self._attr_unique_id = f"{device_uuid}_{key}"
        self._attr_translation_key = key

        charge_point = coordinator.data[device_uuid]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_uuid)},
            manufacturer=MANUFACTURER,
            model=charge_point.model,
            name=DEVICE_NAME,
            serial_number=(
                str(charge_point.serialNumber)
                if charge_point.serialNumber is not None
                else None
            ),
            sw_version=charge_point.firmwareVersion,
        )

    @property
    def device_uuid(self) -> str:
        """Return the charge point's device UUID."""
        return self._device_uuid

    @property
    def charge_point(self) -> OnboardedChargePoint | None:
        """Return the latest data for this charge point, if it is still present."""
        return self.coordinator.data.get(self._device_uuid)

    @property
    def available(self) -> bool:
        """Return whether the entity is available."""
        return super().available and self.charge_point is not None
