"""Models for the data returned by the Octopus charge point API.

Field names mirror the GraphQL schema, hence the camelCase.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, field_validator


class ChargePointOnboarding(BaseModel):
    """Onboarding details for a charge point."""

    accountNumber: Any
    propertyId: Any
    onboardedAt: str | None = None
    externalDeviceId: str | None = None


class ChargePointConfiguration(BaseModel):
    """User-controllable configuration of a charge point."""

    isRandomDelayEnabled: bool | None = None
    isConnected: bool | None = None
    LEDBrightnessPercentage: int | None = None
    isChargeCableAutoLockAvailable: bool | None = None
    isChargeCableAutoLockEnabled: bool | None = None
    isEcoModeEnabled: bool | None = None
    isAwayMode: bool | None = None


class OnboardedChargePoint(BaseModel):
    """A charge point, its status and its configuration."""

    deviceUUID: str
    model: str | None = None
    serialNumber: Any = None
    bluetoothLowEnergyPin: str | None = None
    simcardIdentifier: str | None = None
    firmwareVersion: str | None = None
    controlMode: str | None = None
    chargingMethod: str | None = None
    operationalState: str | None = None
    boostEndTime: str | None = None
    onboarding: ChargePointOnboarding | None = None
    configuration: ChargePointConfiguration | None = None


class ChargePointScheduleSetting(BaseModel):
    """A single scheduled period."""

    start: str | None = None
    end: str | None = None
    action: str | None = None


class ChargePointDaySchedule(BaseModel):
    """The scheduled periods for one day of the week."""

    day: str | None = None
    chargePointScheduleSettings: list[ChargePointScheduleSetting] = []

    @field_validator("chargePointScheduleSettings", mode="before")
    @classmethod
    def _default_empty_when_null(cls, value: Any) -> Any:
        """Treat an explicit null the same as a missing list (a day with no periods)."""
        return [] if value is None else value


class ChargePointPowerReading(BaseModel):
    """A live power reading from the charge point."""

    value: float
    unit: str | None = None
