"""Tests for the API models."""

from __future__ import annotations

from custom_components.octopus_charge.api.models import (
    ChargePointDaySchedule,
    OnboardedChargePoint,
)

from ..conftest import DEVICE_UUID, charge_point_data


def test_charge_point_parses_full_response() -> None:
    charge_point = OnboardedChargePoint.model_validate(charge_point_data())

    assert charge_point.deviceUUID == DEVICE_UUID
    assert charge_point.configuration is not None
    assert charge_point.configuration.LEDBrightnessPercentage == 80
    assert charge_point.onboarding is not None
    assert charge_point.onboarding.propertyId == "12345"


def test_charge_point_tolerates_missing_optional_fields() -> None:
    charge_point = OnboardedChargePoint.model_validate({"deviceUUID": DEVICE_UUID})

    assert charge_point.operationalState is None
    assert charge_point.configuration is None


def test_null_schedule_settings_become_empty_list() -> None:
    schedule = ChargePointDaySchedule.model_validate(
        {"day": "TUESDAY", "chargePointScheduleSettings": None}
    )

    assert schedule.chargePointScheduleSettings == []


def test_missing_schedule_settings_default_to_empty_list() -> None:
    assert ChargePointDaySchedule.model_validate({"day": "TUESDAY"}).day == "TUESDAY"
