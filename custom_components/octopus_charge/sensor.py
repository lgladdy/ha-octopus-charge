"""Sensors for the Octopus Charge integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import (
    async_track_entity_registry_updated_event,
    async_track_state_change_event,
    async_track_state_report_event,
)
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

    def _factory(device_uuid: str) -> list[SensorEntity]:
        live_power = OctopusChargeLivePowerSensor(coordinator, device_uuid)
        return [
            *(
                OctopusChargeEnumSensor(coordinator, device_uuid, description)
                for description in ENUM_SENSORS
            ),
            OctopusChargeBoostEndTimeSensor(coordinator, device_uuid),
            live_power,
            OctopusChargeEnergySensor(
                coordinator, device_uuid, live_power.unique_id or ""
            ),
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
            return datetime.fromisoformat(charge_point.boostEndTime)
        except ValueError:
            return None


class OctopusChargeLivePowerSensor(OctopusChargeEntity, SensorEntity):
    """The charger's instantaneous power draw.

    Unlike every other entity this is push driven rather than polled: it opens a
    live connection to Octopus and streams readings as they arrive, only while the
    charger is actually charging. Deliberately not restored - a stale "last known
    kW" surviving a restart would be misleading for something this transient.
    """

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    # Not charging genuinely means no power is being drawn - a real, known
    # value rather than an absence of data.
    _attr_native_value = 0

    def __init__(self, coordinator: OctopusChargeCoordinator, device_uuid: str) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, device_uuid, "live_power")
        self._stream_task: asyncio.Task[None] | None = None

    async def async_added_to_hass(self) -> None:
        """Start streaming straight away if the charger is already charging."""
        await super().async_added_to_hass()
        self._async_sync_stream()

    async def async_will_remove_from_hass(self) -> None:
        """Stop streaming."""
        self._async_stop_stream()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._async_sync_stream()
        super()._handle_coordinator_update()

    @callback
    def _async_sync_stream(self) -> None:
        """Start/stop the stream to match the charger's operational state."""
        charge_point = self.charge_point
        is_charging = (
            charge_point is not None
            and charge_point.operationalState in CHARGING_STATES
        )
        is_streaming = self._stream_task is not None and not self._stream_task.done()

        if is_charging and not is_streaming:
            _LOGGER.debug("Starting live power stream for %s", self._device_uuid)
            self._stream_task = (
                self.coordinator.config_entry.async_create_background_task(
                    self.hass, self._async_stream_power(), name=f"{DOMAIN} live power"
                )
            )
        elif not is_charging and is_streaming:
            _LOGGER.debug(
                "Stopping live power stream for %s - no longer charging",
                self._device_uuid,
            )
            self._async_stop_stream()
            self._attr_native_value = 0

    @callback
    def _async_stop_stream(self) -> None:
        if self._stream_task is not None and not self._stream_task.done():
            self._stream_task.cancel()
        self._stream_task = None

    async def _async_stream_power(self) -> None:
        """Consume the stream, reconnecting with backoff while it keeps dropping."""
        delay = STREAM_INITIAL_RECONNECT_DELAY_SECONDS
        while True:
            try:
                async for reading in self.coordinator.client.async_stream_power(
                    self.coordinator.account_number, self._device_uuid
                ):
                    delay = STREAM_INITIAL_RECONNECT_DELAY_SECONDS
                    # No reading (e.g. charging just stopped) means 0kW.
                    self._attr_native_value = reading.value if reading else 0
                    self.async_write_ha_state()

                # Stream ended cleanly. If the charger is still charging the next
                # coordinator update starts a new one.
                return
            except asyncio.CancelledError:
                raise
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


class OctopusChargeEnergySensor(OctopusChargeEntity, RestoreSensor):
    """The charger's cumulative energy use, for the Energy dashboard.

    Octopus's API doesn't expose a live cumulative energy counter for the charger
    - only a per-session total once a session has fully ended - so this integrates
    the live power sensor's readings over time instead, the same Riemann-sum
    approach Home Assistant's built-in "Integration - Riemann sum integral" helper
    uses, just built in.

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
        live_power_unique_id: str,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, device_uuid, "energy")
        self._live_power_unique_id = live_power_unique_id
        self._live_power_entity_id: str | None = None
        self._unsub_registry_wait: Callable[[], None] | None = None
        self._last_reading_at: datetime | None = None
        self._last_power_kw: float | None = None
        # Accumulated at full precision; the state is rounded for display only,
        # so rounding error doesn't build up over many small integration steps.
        self._total_kwh: float | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the running total and start tracking live power."""
        await super().async_added_to_hass()

        last_sensor_data = await self.async_get_last_sensor_data()
        last_state = await self.async_get_last_state()
        if (
            last_state is not None
            and last_sensor_data is not None
            and last_state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
        ):
            try:
                self._total_kwh = float(last_sensor_data.native_value)  # type: ignore[arg-type]
            except TypeError, ValueError:
                self._total_kwh = None

        if self._total_kwh is None:
            self._total_kwh = 0.0
        self._attr_native_value = round(self._total_kwh, 4)

        self._async_start_tracking()

    @callback
    def _async_start_tracking(self) -> None:
        """Track the live power sensor, resolving it via the entity registry.

        Resolved by unique id so this stays correct if the user renames the live
        power entity.
        """
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id(
            Platform.SENSOR, DOMAIN, self._live_power_unique_id
        )
        if entity_id is not None:
            self._async_track(entity_id)
            return

        # On a brand new install live power is added alongside us and may not be
        # in the registry yet - wait for it to appear.
        @callback
        def _registry_updated(event: Event[Any]) -> None:
            if event.data.get("action") != "create":
                return

            if (
                found := registry.async_get_entity_id(
                    Platform.SENSOR, DOMAIN, self._live_power_unique_id
                )
            ) is not None:
                self._async_cancel_registry_wait()
                self._async_track(found)

        self._unsub_registry_wait = self.hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED, _registry_updated
        )
        self.async_on_remove(self._async_cancel_registry_wait)

    @callback
    def _async_cancel_registry_wait(self) -> None:
        if self._unsub_registry_wait is not None:
            self._unsub_registry_wait()
            self._unsub_registry_wait = None

    @callback
    def _async_track(self, entity_id: str) -> None:
        self._live_power_entity_id = entity_id
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [entity_id], self._async_update_from_power
            )
        )
        # A reading identical to the previous one doesn't change the state, so
        # Home Assistant reports it rather than firing a state change. Those
        # still mark time passing at that power, so they must be integrated too
        # or a steady charge would be credited as nothing.
        self.async_on_remove(
            async_track_state_report_event(
                self.hass, [entity_id], self._async_update_from_power
            )
        )
        # If the user renames the live power entity while running, the entity id
        # we resolved goes stale - reload to pick up the new one.
        self.async_on_remove(
            async_track_entity_registry_updated_event(
                self.hass, [entity_id], self._async_live_power_registry_updated
            )
        )

    @callback
    def _async_live_power_registry_updated(self, event: Event[Any]) -> None:
        data = event.data
        if data["action"] != "update" or "entity_id" not in data["changes"]:
            return

        _LOGGER.debug(
            "Live power entity renamed from '%s' to '%s' - reloading",
            self._live_power_entity_id,
            data["entity_id"],
        )
        self.hass.config_entries.async_schedule_reload(
            self.coordinator.config_entry.entry_id
        )

    @callback
    def _async_update_from_power(self, event: Event[Any]) -> None:
        """Integrate the live power sensor's latest reading into the total."""
        new_state = event.data.get("new_state")
        power_kw: float | None
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            power_kw = None
        else:
            try:
                power_kw = float(new_state.state)
            except TypeError, ValueError:
                power_kw = None

        now = dt_util.utcnow()
        # Integrate using the *previous* reading's power over the window that has
        # just elapsed - that's what was actually being drawn during it. Using the
        # new value instead would integrate the step where charging stops (power
        # drops to 0) as elapsed_time * 0, discarding real energy that was
        # consumed right up until that reading.
        if (
            self._last_reading_at is not None
            and self._last_power_kw is not None
            and self._total_kwh is not None
        ):
            elapsed_hours = (now - self._last_reading_at).total_seconds() / 3600
            self._total_kwh = integrate_energy_kwh(
                self._total_kwh, self._last_power_kw, elapsed_hours
            )
            self._attr_native_value = round(self._total_kwh, 4)
            self.async_write_ha_state()

        self._last_reading_at = now
        self._last_power_kw = power_kw


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
