"""Diagnostics for the Octopus Charge integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant

from .const import CONF_ACCOUNT_NUMBER
from .coordinator import OctopusChargeConfigEntry

TO_REDACT = {
    CONF_API_KEY,
    CONF_ACCOUNT_NUMBER,
    "title",
    "unique_id",
    "deviceUUID",
    "serialNumber",
    "bluetoothLowEnergyPin",
    "simcardIdentifier",
    "accountNumber",
    "propertyId",
    "externalDeviceId",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: OctopusChargeConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime_data = entry.runtime_data
    coordinator = runtime_data.coordinator
    schedule_coordinator = runtime_data.schedule_coordinator

    return async_redact_data(
        {
            "entry": entry.as_dict(),
            "last_update_success": coordinator.last_update_success,
            "last_retrieved": coordinator.last_retrieved,
            "last_failure": coordinator.last_failure,
            "charge_points": [
                charge_point.model_dump() for charge_point in coordinator.data.values()
            ],
            "schedule_last_update_success": schedule_coordinator.last_update_success,
            "schedules": list((schedule_coordinator.data or {}).values()),
        },
        TO_REDACT,
    )
