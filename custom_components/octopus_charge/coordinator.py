"""Data update coordinators for the Octopus Charge integration."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api.client import OctopusChargeApiClient
from .api.exceptions import OctopusChargeAuthError, OctopusChargeError
from .api.models import OnboardedChargePoint
from .const import (
    BURST_DURATION_SECONDS,
    BURST_INTERVAL_SECONDS,
    CONF_ACCOUNT_NUMBER,
    DOMAIN,
    SCAN_INTERVAL,
    SCHEDULE_BURST_JITTER_MAX_SECONDS,
    SCHEDULE_BURST_LEAD_SECONDS,
    SCHEDULE_SCAN_INTERVAL,
)
from .utils.backoff import retry_after_seconds
from .utils.schedule import compute_next_schedule_transition

_LOGGER = logging.getLogger(__name__)

type OctopusChargeConfigEntry = ConfigEntry[OctopusChargeRuntimeData]

# Schedule as exposed to entities: day name -> list of {start, end, action}.
type ScheduleByDay = dict[str, list[dict[str, Any]]]


@dataclass
class OctopusChargeRuntimeData:
    """Objects shared by the platforms of a config entry."""

    client: OctopusChargeApiClient
    coordinator: OctopusChargeCoordinator
    schedule_coordinator: OctopusChargeScheduleCoordinator


class OctopusChargeCoordinator(DataUpdateCoordinator[dict[str, OnboardedChargePoint]]):
    """Polls the status and configuration of the account's charge points."""

    config_entry: OctopusChargeConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: OctopusChargeConfigEntry,
        client: OctopusChargeApiClient,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self.client = client
        self.account_number: str = entry.data[CONF_ACCOUNT_NUMBER]
        # When the data was last successfully retrieved from Octopus.
        self.last_retrieved: datetime | None = None

        self._property_ids: list[str] | None = None
        self._consecutive_failures = 0
        self._burst_task: asyncio.Task[None] | None = None
        self._last_burst_started_at: datetime | None = None

    async def _async_update_data(self) -> dict[str, OnboardedChargePoint]:
        """Fetch the account's charge points."""
        try:
            if self._property_ids is None:
                property_ids = await self.client.async_get_property_ids(
                    self.account_number
                )
                if property_ids is None:
                    raise ConfigEntryError(
                        f"Account {self.account_number} could not be found"
                    )
                self._property_ids = property_ids

            charge_points: dict[str, OnboardedChargePoint] = {}
            for property_id in self._property_ids:
                for charge_point in await self.client.async_get_charge_points(
                    self.account_number, property_id
                ):
                    charge_points[charge_point.deviceUUID] = charge_point
        except OctopusChargeAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except OctopusChargeError as err:
            self._consecutive_failures += 1
            raise UpdateFailed(
                f"Error retrieving charge point data: {err}",
                retry_after=retry_after_seconds(
                    self._consecutive_failures, SCAN_INTERVAL.total_seconds()
                ),
            ) from err

        self._consecutive_failures = 0
        self.last_retrieved = dt_util.utcnow()
        return charge_points

    @callback
    def async_start_burst_refresh(
        self,
        interval_seconds: int = BURST_INTERVAL_SECONDS,
        duration_seconds: int = BURST_DURATION_SECONDS,
    ) -> None:
        """(Re)start a short burst of frequent polling.

        Used right after something is expected to change on the charger (boost
        toggled, a scheduled period starting/ending) so dependent entities - like
        live power - pick up the change quickly rather than waiting up to a full
        scan interval.

        This is the one place every trigger funnels through, so it also guards
        against toggling boost rapidly to sustain fast polling indefinitely: a
        call within ``interval_seconds`` of the previous one is ignored.
        """
        now = dt_util.utcnow()
        if (
            self._last_burst_started_at is not None
            and (now - self._last_burst_started_at).total_seconds() < interval_seconds
        ):
            return

        self._last_burst_started_at = now
        if self._burst_task is not None and not self._burst_task.done():
            self._burst_task.cancel()

        self._burst_task = self.config_entry.async_create_background_task(
            self.hass,
            self._async_run_burst(interval_seconds, duration_seconds),
            name=f"{DOMAIN} burst refresh",
        )

    async def _async_run_burst(
        self, interval_seconds: int, duration_seconds: int
    ) -> None:
        # Each refresh below also reschedules the normal poll, so for the
        # duration of the burst this loop is the only thing polling, and the
        # normal schedule resumes by itself once it ends.
        end = dt_util.utcnow() + timedelta(seconds=duration_seconds)
        while dt_util.utcnow() < end:
            # Wait a full interval before polling - the change we're bursting
            # around (e.g. a boost toggle) hasn't necessarily propagated on
            # Octopus's side yet, so an immediate poll can catch the charger
            # still in its pre-change state and read as an instant revert.
            await asyncio.sleep(interval_seconds)

            # Already backed off after repeated failures - don't keep hammering
            # a server that's failing; the normal retry schedule takes over.
            if self._consecutive_failures > 0:
                break

            await self.async_refresh()

    async def async_shutdown(self) -> None:
        """Cancel any in-flight burst and shut down."""
        if self._burst_task is not None and not self._burst_task.done():
            self._burst_task.cancel()
        await super().async_shutdown()


class OctopusChargeScheduleCoordinator(DataUpdateCoordinator[dict[str, ScheduleByDay]]):
    """Polls each charge point's weekly schedule.

    Also arms a timer for each charge point's next scheduled start/end so the
    status coordinator can burst-refresh right as it happens.
    """

    config_entry: OctopusChargeConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: OctopusChargeConfigEntry,
        client: OctopusChargeApiClient,
        status_coordinator: OctopusChargeCoordinator,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} schedule",
            update_interval=SCHEDULE_SCAN_INTERVAL,
        )
        self.client = client
        self.account_number: str = entry.data[CONF_ACCOUNT_NUMBER]
        self._status_coordinator = status_coordinator
        self._consecutive_failures = 0
        self._cancel_timers: dict[str, Callable[[], None]] = {}

    async def _async_update_data(self) -> dict[str, ScheduleByDay]:
        """Fetch the weekly schedule of every known charge point."""
        data: dict[str, ScheduleByDay] = dict(self.data or {})
        try:
            for device_uuid in self._status_coordinator.data or {}:
                schedules = await self.client.async_get_schedules(
                    self.account_number, device_uuid
                )
                if schedules is None:
                    continue

                data[device_uuid] = {
                    day.day: [
                        {
                            "start": setting.start,
                            "end": setting.end,
                            "action": setting.action,
                        }
                        for setting in day.chargePointScheduleSettings
                    ]
                    for day in schedules
                    if day.day is not None
                }
        except OctopusChargeAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except OctopusChargeError as err:
            self._consecutive_failures += 1
            raise UpdateFailed(
                f"Error retrieving charge point schedule: {err}",
                retry_after=retry_after_seconds(
                    self._consecutive_failures, SCHEDULE_SCAN_INTERVAL.total_seconds()
                ),
            ) from err

        self._consecutive_failures = 0
        for device_uuid, schedule in data.items():
            self._async_arm_transition_timer(device_uuid, schedule)

        return data

    @callback
    def _async_arm_transition_timer(
        self, device_uuid: str, schedule: ScheduleByDay
    ) -> None:
        """(Re)arm the timer for a charge point's next scheduled start/end."""
        if (cancel := self._cancel_timers.pop(device_uuid, None)) is not None:
            cancel()

        next_transition = compute_next_schedule_transition(schedule, dt_util.now())
        if next_transition is None:
            return

        jitter = random.uniform(0, SCHEDULE_BURST_JITTER_MAX_SECONDS)
        fire_at = next_transition + timedelta(
            seconds=jitter - SCHEDULE_BURST_LEAD_SECONDS
        )

        @callback
        def _on_transition(_now: datetime) -> None:
            self._status_coordinator.async_start_burst_refresh()
            # The schedule itself hasn't changed, so the next boundary can be
            # armed straight away rather than waiting for the next poll.
            self._async_arm_transition_timer(device_uuid, schedule)

        self._cancel_timers[device_uuid] = async_track_point_in_time(
            self.hass, _on_transition, fire_at
        )

    async def async_shutdown(self) -> None:
        """Cancel any armed timers and shut down."""
        for cancel in self._cancel_timers.values():
            cancel()
        self._cancel_timers.clear()
        await super().async_shutdown()
