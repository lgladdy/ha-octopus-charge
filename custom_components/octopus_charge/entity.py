"""Base entity and helpers for the Octopus Charge integration."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api.exceptions import OctopusChargeAuthError, OctopusChargeError
from .api.models import OnboardedChargePoint
from .const import DEVICE_NAME, DOMAIN, MANUFACTURER, OPTIMISTIC_TIMEOUT
from .coordinator import OctopusChargeConfigEntry, OctopusChargeCoordinator

_LOGGER = logging.getLogger(__name__)


@callback
def async_add_charge_point_entities(
    entry: OctopusChargeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: Callable[[str], Iterable[Entity]],
) -> None:
    """Add entities for every charge point, including ones that appear later.

    The factory runs on every update rather than once per charge point, so an
    entity it only creates once the charger reports support for it (e.g. cable
    auto lock, when the configuration first comes back empty) still appears.
    """
    coordinator = entry.runtime_data.coordinator
    known: set[str | None] = set()

    @callback
    def _async_add_new() -> None:
        new_entities = [
            entity
            for device_uuid in coordinator.data
            for entity in factory(device_uuid)
            if entity.unique_id not in known
        ]
        if not new_entities:
            return

        known.update(entity.unique_id for entity in new_entities)
        async_add_entities(new_entities)

    _async_add_new()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new))


class OptimisticValue[T]:
    """Holds a value we've just sent to the charger until Octopus reflects it.

    After a command, Octopus can take a while to reflect the change, so polls -
    including one already in flight when the command was sent - can read the old
    value back and make the entity flip-flop. The sent value is shown until a
    poll completed after the command returns it, or until OPTIMISTIC_TIMEOUT
    passes without one doing so (the command evidently didn't stick), after which
    the fetched value wins again. Data from before the command never confirms it:
    after "off" then "on" in quick succession, the cached pre-command "on" would
    otherwise end the wait for the second command early.
    """

    def __init__(self) -> None:
        """Initialise with nothing pending."""
        self._value: T | None = None
        self._set_at: datetime | None = None
        self._expires: datetime | None = None

    def set(self, value: T) -> None:
        """Record a value that has just been sent to the charger."""
        self._value = value
        self._set_at = dt_util.utcnow()
        self._expires = self._set_at + OPTIMISTIC_TIMEOUT

    def resolve(self, fetched: T | None, retrieved_at: datetime | None) -> T | None:
        """Return the value to show, given the fetched value and when it arrived."""
        if self._expires is not None and self._set_at is not None:
            confirmed = (
                fetched == self._value
                and retrieved_at is not None
                and retrieved_at > self._set_at
            )
            if not confirmed and dt_util.utcnow() < self._expires:
                return self._value

            self._expires = None
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

    async def _async_send(self, command: Awaitable[None]) -> None:
        """Send a command to the charger, reporting failure as a HA error."""
        try:
            await command
        except OctopusChargeAuthError as err:
            self.coordinator.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                "Octopus Energy rejected the API key - re-authenticate the integration"
            ) from err
        except OctopusChargeError as err:
            # The UI only shows the message; the details (status, request ids,
            # timing) are for the logs.
            _LOGGER.warning("Command to the charger failed: %s", err.describe())
            raise HomeAssistantError(
                f"Octopus Energy didn't accept the change: {err}"
            ) from err

    @property
    def available(self) -> bool:
        """Return whether the entity is available."""
        return super().available and self.charge_point is not None
