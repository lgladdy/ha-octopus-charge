"""Switches for the Octopus Charge integration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .api.client import OctopusChargeApiClient
from .api.models import OnboardedChargePoint
from .const import BOOSTING_STATES, DEFAULT_BOOST_DURATION
from .coordinator import OctopusChargeConfigEntry, OctopusChargeCoordinator
from .entity import (
    OctopusChargeEntity,
    OptimisticValue,
    async_add_charge_point_entities,
)

PARALLEL_UPDATES = 1

SERVICE_BOOST_CHARGE_POINT = "boost_charge_point"


@dataclass(frozen=True, kw_only=True)
class OctopusChargeSwitchDescription(SwitchEntityDescription):
    """Describes a switch backed by a boolean charge point setting."""

    value_fn: Callable[[OnboardedChargePoint], bool | None]
    set_fn: Callable[[OctopusChargeApiClient, str, str, bool], Awaitable[None]]
    # Only create the switch if the charge point supports it.
    supported_fn: Callable[[OnboardedChargePoint], bool] = lambda _: True


def _configuration_value(
    attribute: str,
) -> Callable[[OnboardedChargePoint], bool | None]:
    def _value(charge_point: OnboardedChargePoint) -> bool | None:
        if charge_point.configuration is None:
            return None

        return getattr(charge_point.configuration, attribute)

    return _value


SWITCHES: tuple[OctopusChargeSwitchDescription, ...] = (
    OctopusChargeSwitchDescription(
        key="eco_mode",
        value_fn=_configuration_value("isEcoModeEnabled"),
        set_fn=lambda client, account, device, enabled: client.async_set_eco_mode(
            account, device, enabled
        ),
    ),
    OctopusChargeSwitchDescription(
        key="random_delay",
        value_fn=_configuration_value("isRandomDelayEnabled"),
        set_fn=lambda client, account, device, enabled: client.async_set_random_delay(
            account, device, enabled
        ),
    ),
    OctopusChargeSwitchDescription(
        key="away_mode",
        value_fn=_configuration_value("isAwayMode"),
        set_fn=lambda client, account, device, enabled: client.async_set_away_mode(
            account, device, enabled
        ),
    ),
    OctopusChargeSwitchDescription(
        key="cable_auto_lock",
        value_fn=_configuration_value("isChargeCableAutoLockEnabled"),
        set_fn=lambda client, account, device, enabled: (
            client.async_set_cable_auto_lock(account, device, enabled)
        ),
        supported_fn=lambda charge_point: bool(
            charge_point.configuration
            and charge_point.configuration.isChargeCableAutoLockAvailable
        ),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OctopusChargeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the switches."""
    coordinator = entry.runtime_data.coordinator

    def _factory(device_uuid: str) -> list[SwitchEntity]:
        charge_point = coordinator.data[device_uuid]
        return [
            *(
                OctopusChargeSettingSwitch(coordinator, device_uuid, description)
                for description in SWITCHES
                if description.supported_fn(charge_point)
            ),
            OctopusChargeBoostSwitch(coordinator, device_uuid),
        ]

    async_add_charge_point_entities(entry, async_add_entities, _factory)

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_BOOST_CHARGE_POINT,
        {
            vol.Required("hours"): cv.positive_int,
            vol.Required("minutes"): cv.positive_int,
        },
        "async_boost_charge_point",
    )


class OctopusChargeSettingSwitch(OctopusChargeEntity, SwitchEntity):
    """A switch for a boolean charge point setting."""

    entity_description: OctopusChargeSwitchDescription

    def __init__(
        self,
        coordinator: OctopusChargeCoordinator,
        device_uuid: str,
        description: OctopusChargeSwitchDescription,
    ) -> None:
        """Initialise the switch."""
        super().__init__(coordinator, device_uuid, description.key)
        self.entity_description = description
        self._optimistic = OptimisticValue[bool]()

    @property
    def is_on(self) -> bool | None:
        """Return whether the setting is on."""
        charge_point = self.charge_point
        fetched = (
            self.entity_description.value_fn(charge_point) if charge_point else None
        )
        return self._optimistic.resolve(fetched, self.coordinator.last_retrieved)

    async def _async_set(self, enabled: bool) -> None:
        await self.entity_description.set_fn(
            self.coordinator.client,
            self.coordinator.account_number,
            self._device_uuid,
            enabled,
        )
        self._optimistic.set(enabled)
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the setting on."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the setting off."""
        await self._async_set(False)


class OctopusChargeBoostSwitch(OctopusChargeEntity, SwitchEntity):
    """Starts and stops a boost charge."""

    def __init__(self, coordinator: OctopusChargeCoordinator, device_uuid: str) -> None:
        """Initialise the switch."""
        super().__init__(coordinator, device_uuid, "boost")
        self._optimistic = OptimisticValue[bool]()

    @property
    def is_on(self) -> bool | None:
        """Return whether the charger is boost charging."""
        charge_point = self.charge_point
        fetched = (
            charge_point.operationalState in BOOSTING_STATES
            if charge_point is not None and charge_point.operationalState is not None
            else None
        )
        return self._optimistic.resolve(fetched, self.coordinator.last_retrieved)

    async def _async_start(self, duration: timedelta) -> None:
        await self.coordinator.client.async_start_boost(
            self.coordinator.account_number,
            self._device_uuid,
            dt_util.utcnow() + duration,
        )
        self._optimistic.set(True)
        self.async_write_ha_state()
        # We already know the operational state is about to change - poll for
        # the real confirmation much sooner than the normal scan interval, so
        # dependent entities (e.g. live power) don't lag behind.
        self.coordinator.async_start_burst_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start a boost charge for the default duration."""
        await self._async_start(DEFAULT_BOOST_DURATION)

    async def async_boost_charge_point(self, hours: int, minutes: int) -> None:
        """Start a boost charge for a specific duration (boost_charge_point action)."""
        await self._async_start(timedelta(hours=hours, minutes=minutes))

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the boost charge."""
        await self.coordinator.client.async_stop_boost(
            self.coordinator.account_number, self._device_uuid
        )
        self._optimistic.set(False)
        self.async_write_ha_state()
        self.coordinator.async_start_burst_refresh()
