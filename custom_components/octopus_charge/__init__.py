"""The Octopus Charge integration."""

from __future__ import annotations

from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.loader import async_get_loaded_integration

from .api.client import OctopusChargeApiClient
from .const import DOMAIN
from .coordinator import (
    OctopusChargeConfigEntry,
    OctopusChargeCoordinator,
    OctopusChargeRuntimeData,
    OctopusChargeScheduleCoordinator,
)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


@callback
def _keep_polling() -> None:
    """Listener that keeps the schedule coordinator polling."""


async def async_setup_entry(
    hass: HomeAssistant, entry: OctopusChargeConfigEntry
) -> bool:
    """Set up Octopus Charge from a config entry."""
    version = async_get_loaded_integration(hass, DOMAIN).version
    client = OctopusChargeApiClient(
        async_get_clientsession(hass),
        entry.data[CONF_API_KEY],
        user_agent=f"ha-octopus-charge/{version}",
    )

    coordinator = OctopusChargeCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    # The schedule is a nice-to-have; don't fail setup if it can't be retrieved
    # yet - it retries on its own schedule.
    schedule_coordinator = OctopusChargeScheduleCoordinator(
        hass, entry, client, coordinator
    )
    await schedule_coordinator.async_refresh()
    # A coordinator only polls while something listens to it, and the schedule
    # also drives the schedule boundary bursts - keep it polling even if the
    # Schedule sensor (its only entity) is disabled.
    entry.async_on_unload(schedule_coordinator.async_add_listener(_keep_polling))

    entry.runtime_data = OctopusChargeRuntimeData(
        client=client,
        coordinator=coordinator,
        schedule_coordinator=schedule_coordinator,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: OctopusChargeConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
