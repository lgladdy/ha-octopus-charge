"""Sensors for the Octopus Charge integration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .api.exceptions import OctopusChargeError
from .api.models import OnboardedChargePoint
from .const import (
    CHARGING_METHODS,
    CHARGING_STATES,
    DOMAIN,
    OPERATIONAL_STATES,
    STREAM_INITIAL_RECONNECT_DELAY_SECONDS,
    STREAM_MAX_RECONNECT_DELAY_SECONDS,
)
from .coordinator import (
    OctopusChargeConfigEntry,
    OctopusChargeCoordinator,
    OctopusChargeScheduleCoordinator,
)
from .entity import OctopusChargeEntity, async_add_charge_point_entities
from .utils.energy import integrate_energy_kwh

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class OctopusChargeEnumSensorDescription(SensorEntityDescription):
    """Describes a sensor that reports one of a fixed set of values."""

    value_fn: Callable[[OnboardedChargePoint], str | None]


ENUM_SENSORS: tuple[OctopusChargeEnumSensorDescription, ...] = (
    OctopusChargeEnumSensorDescription(
        key="operational_state",
        device_class=SensorDeviceClass.ENUM,
        options=OPERATIONAL_STATES,
        value_fn=lambda charge_point: charge_point.operationalState,
    ),
    OctopusChargeEnumSensorDescription(
        key="charging_method",
        device_class=SensorDeviceClass.ENUM,
        options=CHARGING_METHODS,
        value_fn=lambda charge_point: charge_point.chargingMethod,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OctopusChargeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors."""
    coordinator = entry.runtime_data.coordinator

    monitors: dict[str, OctopusChargePowerMonitor] = {}

    def _factory(device_uuid: str) -> list[SensorEntity]:
        if (monitor := monitors.get(device_uuid)) is None:
            monitor = monitors[device_uuid] = OctopusChargePowerMonitor(
                coordinator, device_uuid
            )
        return [
            *(
                OctopusChargeEnumSensor(coordinator, device_uuid, description)
                for description in ENUM_SENSORS
            ),
            OctopusChargeBoostEndTimeSensor(coordinator, device_uuid),
            OctopusChargeLivePowerSensor(coordinator, device_uuid, monitor),
            OctopusChargeEnergySensor(coordinator, device_uuid, monitor),
            OctopusChargeScheduleSensor(
                coordinator, entry.runtime_data.schedule_coordinator, device_uuid
            ),
        ]

    async_add_charge_point_entities(entry, async_add_entities, _factory)


class OctopusChargeEnumSensor(OctopusChargeEntity, SensorEntity):
    """A sensor reporting one of a fixed set of values from the charge point.

    If Octopus ever reports a value we don't know about, the last known good
    value is kept (and a warning logged once) - assigning a value outside an enum
    sensor's options would otherwise raise when the state is written.
    """

    entity_description: OctopusChargeEnumSensorDescription

    def __init__(
        self,
        coordinator: OctopusChargeCoordinator,
        device_uuid: str,
        description: OctopusChargeEnumSensorDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, device_uuid, description.key)
        self.entity_description = description
        self._unrecognized_value: str | None = None

    async def async_added_to_hass(self) -> None:
        """Take the current value once added (not on construction)."""
        await super().async_added_to_hass()
        self._update_value()

    def _update_value(self) -> None:
        charge_point = self.charge_point
        value = (
            self.entity_description.value_fn(charge_point)
            if charge_point is not None
            else None
        )
        if value is None:
            return

        if value in (self.entity_description.options or []):
            self._attr_native_value = value
            self._unrecognized_value = None
        elif self._unrecognized_value != value:
            self._unrecognized_value = value
            _LOGGER.warning(
                "Received unrecognized %s '%s' - keeping previous state",
                self.entity_description.key,
                value,
            )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_value()
        super()._handle_coordinator_update()


class OctopusChargeBoostEndTimeSensor(OctopusChargeEntity, SensorEntity):
    """When the current boost charge is due to end (unknown outside a boost)."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: OctopusChargeCoordinator, device_uuid: str) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, device_uuid, "boost_end_time")

    @property
    def native_value(self) -> datetime | None:
        """Return when the boost ends."""
        charge_point = self.charge_point
        if charge_point is None or charge_point.boostEndTime is None:
            return None

        try:
            end = dt_util.parse_datetime(charge_point.boostEndTime)
        except ValueError:
            return None
        if end is None:
            return None
        # A timestamp sensor needs an aware value; Kraken works in UTC.
        return end if end.tzinfo is not None else end.replace(tzinfo=UTC)


class OctopusChargePowerMonitor:
    """Streams one charge point's live power while it is charging.

    Unlike everything else this is push driven rather than polled: it holds a
    live connection to Octopus open while the charger is charging, and tells its
    listeners about each reading as it arrives. Both the live power and energy
    sensors listen, so energy is integrated from the readings themselves - not
    from the live power entity's state, which is also re-written on every poll
    and is shown in whatever unit the user picks - and keeps counting even if
    the live power entity is disabled.
    """

    def __init__(self, coordinator: OctopusChargeCoordinator, device_uuid: str) -> None:
        """Initialise the monitor."""
        self._coordinator = coordinator
        self._device_uuid = device_uuid
        # Not charging genuinely means no power is being drawn - a real, known
        # value rather than an absence of data.
        self.power_kw: float = 0
        self._listeners: list[Callable[[], None]] = []
        self._unsub_coordinator: Callable[[], None] | None = None
        self._stream_task: asyncio.Task[None] | None = None

    @callback
    def async_add_listener(
        self, update_callback: Callable[[], None]
    ) -> Callable[[], None]:
        """Listen for power changes; streams while anything is listening."""
        self._listeners.append(update_callback)
        if self._unsub_coordinator is None:
            self._unsub_coordinator = self._coordinator.async_add_listener(
                self._async_sync_stream
            )
            self._async_sync_stream()

        @callback
        def _remove() -> None:
            self._listeners.remove(update_callback)
            if not self._listeners and self._unsub_coordinator is not None:
                self._unsub_coordinator()
                self._unsub_coordinator = None
                self._async_stop_stream()

        return _remove

    @callback
    def _async_set_power(self, power_kw: float) -> None:
        self.power_kw = power_kw
        for update_callback in list(self._listeners):
            update_callback()

    @callback
    def _async_sync_stream(self) -> None:
        """Start/stop the stream to match the charger's operational state."""
        charge_point = self._coordinator.data.get(self._device_uuid)
        if charge_point is not None and charge_point.operationalState in (
            CHARGING_STATES
        ):
            if self._stream_task is None or self._stream_task.done():
                _LOGGER.debug("Starting live power stream for %s", self._device_uuid)
                self._stream_task = (
                    self._coordinator.config_entry.async_create_background_task(
                        self._coordinator.hass,
                        self._async_stream_power(),
                        name=f"{DOMAIN} live power",
                    )
                )
            return

        # Not charging: no power is being drawn, whatever the last reading was
        # and whether or not a stream is still open.
        self._async_stop_stream()
        if self.power_kw != 0:
            self._async_set_power(0)

    @callback
    def _async_stop_stream(self) -> None:
        if self._stream_task is not None and not self._stream_task.done():
            _LOGGER.debug("Stopping live power stream for %s", self._device_uuid)
            self._stream_task.cancel()
        self._stream_task = None

    async def _async_stream_power(self) -> None:
        """Consume the stream, reconnecting with backoff while still charging.

        Runs until cancelled: the coordinator stops it once the charger is no
        longer charging, including after the stream has ended by itself.
        """
        delay = STREAM_INITIAL_RECONNECT_DELAY_SECONDS
        while True:
            try:
                async for reading in self._coordinator.client.async_stream_power(
                    self._coordinator.account_number, self._device_uuid
                ):
                    delay = STREAM_INITIAL_RECONNECT_DELAY_SECONDS
                    # No reading (e.g. charging just stopped) means 0kW.
                    self._async_set_power(reading.value if reading else 0)

                _LOGGER.debug(
                    "Live power stream for %s ended. Reconnecting in %ss",
                    self._device_uuid,
                    delay,
                )
            except OctopusChargeError as err:
                _LOGGER.debug(
                    "Live power stream for %s dropped: %s. Reconnecting in %ss",
                    self._device_uuid,
                    err,
                    delay,
                )
            except Exception:
                _LOGGER.exception(
                    "Unexpected error in live power stream for %s. Reconnecting in %ss",
                    self._device_uuid,
                    delay,
                )

            await asyncio.sleep(delay)
            delay = min(delay * 2, STREAM_MAX_RECONNECT_DELAY_SECONDS)


class OctopusChargeLivePowerSensor(OctopusChargeEntity, SensorEntity):
    """The charger's instantaneous power draw, 0 when not charging.

    Deliberately not restored - a stale "last known kW" surviving a restart would
    be misleading for something this transient.
    """

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: OctopusChargeCoordinator,
        device_uuid: str,
        monitor: OctopusChargePowerMonitor,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, device_uuid, "live_power")
        self._monitor = monitor

    async def async_added_to_hass(self) -> None:
        """Follow the live power monitor."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._monitor.async_add_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        """Push driven, so a failed status poll doesn't make readings unavailable."""
        return self.charge_point is not None

    @property
    def native_value(self) -> float:
        """Return the latest power reading."""
        return self._monitor.power_kw


class OctopusChargeEnergySensor(OctopusChargeEntity, RestoreSensor):
    """The charger's cumulative energy use, for the Energy dashboard.

    Octopus's API doesn't expose a live cumulative energy counter for the charger
    - only a per-session total once a session has fully ended - so this integrates
    the live power readings over time instead, the same Riemann-sum approach Home
    Assistant's built-in "Integration - Riemann sum integral" helper uses, just
    built in.

    Starts from 0 when first added - there's no way to know the charger's actual
    lifetime total from the API.

    The running integration only lives in memory: a restart mid-charge resumes
    from the last saved total rather than the true total at restart time,
    silently losing whatever was drawn during the gap. There's no live power
    history to replay this from afterwards.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        coordinator: OctopusChargeCoordinator,
        device_uuid: str,
        monitor: OctopusChargePowerMonitor,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, device_uuid, "energy")
        self._monitor = monitor
        # Accumulated at full precision; the state is rounded for display only,
        # so rounding error doesn't build up over many small integration steps.
        self._total_kwh = 0.0
        self._last_reading_at: datetime | None = None
        self._last_power_kw: float | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the running total and start integrating live power."""
        await super().async_added_to_hass()

        # The saved total is valid whatever the entity's last state was - it is
        # only "unavailable" because the charger data was, e.g. during an outage
        # or a reauth.
        if (last_sensor_data := await self.async_get_last_sensor_data()) is not None:
            with contextlib.suppress(TypeError, ValueError):
                self._total_kwh = float(last_sensor_data.native_value)  # type: ignore[arg-type]
        self._attr_native_value = round(self._total_kwh, 4)

        self._last_reading_at = dt_util.utcnow()
        self._last_power_kw = self._monitor.power_kw
        self.async_on_remove(
            self._monitor.async_add_listener(self._async_update_from_power)
        )

    @property
    def available(self) -> bool:
        """Calculated locally, so a failed status poll doesn't hide the total."""
        return self.charge_point is not None

    @callback
    def _async_update_from_power(self) -> None:
        """Integrate the window since the previous reading into the total."""
        now = dt_util.utcnow()
        # Integrate using the *previous* reading's power over the window that has
        # just elapsed - that's what was actually being drawn during it. Using the
        # new value instead would integrate the step where charging stops (power
        # drops to 0) as elapsed_time * 0, discarding real energy that was
        # consumed right up until that reading.
        if self._last_reading_at is not None and self._last_power_kw is not None:
            elapsed_hours = (now - self._last_reading_at).total_seconds() / 3600
            self._total_kwh = integrate_energy_kwh(
                self._total_kwh, self._last_power_kw, elapsed_hours
            )
            self._attr_native_value = round(self._total_kwh, 4)
            self.async_write_ha_state()

        self._last_reading_at = now
        self._last_power_kw = self._monitor.power_kw


class OctopusChargeScheduleSensor(OctopusChargeEntity, SensorEntity):
    """A summary of the charger's weekly charging schedule (read only)."""

    def __init__(
        self,
        coordinator: OctopusChargeCoordinator,
        schedule_coordinator: OctopusChargeScheduleCoordinator,
        device_uuid: str,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, device_uuid, "schedule")
        self._schedule_coordinator = schedule_coordinator

    async def async_added_to_hass(self) -> None:
        """Also refresh whenever the schedule changes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._schedule_coordinator.async_add_listener(self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        """Available while we have a schedule for this charge point."""
        return super().available and self._schedule is not None

    @property
    def _schedule(self) -> dict[str, list[dict[str, Any]]] | None:
        return (self._schedule_coordinator.data or {}).get(self._device_uuid)

    @property
    def native_value(self) -> str | None:
        """Return the number of scheduled periods across the week."""
        schedule = self._schedule
        if schedule is None:
            return None

        total = sum(len(periods) for periods in schedule.values())
        return f"{total} period{'s' if total != 1 else ''}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the full schedule."""
        return {"schedule": self._schedule or {}}
