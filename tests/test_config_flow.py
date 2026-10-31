"""Tests for the config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_API_KEY
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.octopus_charge.api.exceptions import (
    OctopusChargeAuthError,
    OctopusChargeConnectionError,
    OctopusChargeServerError,
)
from custom_components.octopus_charge.const import CONF_ACCOUNT_NUMBER, DOMAIN

from .conftest import ACCOUNT_NUMBER, API_KEY, PROPERTY_ID, charge_point


async def _start(hass: HomeAssistant) -> dict:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def _submit(hass: HomeAssistant, flow_id: str, **user_input: str) -> dict:
    return await hass.config_entries.flow.async_configure(flow_id, user_input)


async def test_user_flow_creates_an_entry(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await _submit(
        hass,
        result["flow_id"],
        api_key=f"  {API_KEY} ",
        account_number=" a-12345678 ",
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == ACCOUNT_NUMBER
    assert result["data"] == {
        CONF_API_KEY: API_KEY,
        CONF_ACCOUNT_NUMBER: ACCOUNT_NUMBER,
    }
    assert result["result"].unique_id == ACCOUNT_NUMBER
    mock_client.async_get_charge_points.assert_awaited_with(ACCOUNT_NUMBER, PROPERTY_ID)


@pytest.mark.parametrize(
    ("configure", "error"),
    [
        ("invalid_format", "invalid_account_number"),
        ("not_found", "account_not_found"),
        ("auth", "invalid_auth"),
        ("server", "cannot_connect"),
        ("connection", "cannot_connect"),
        ("unexpected", "unknown"),
        ("no_charger", "no_charge_point"),
        ("no_properties", "no_charge_point"),
    ],
)
async def test_user_flow_errors_and_recovery(
    hass: HomeAssistant, mock_client: AsyncMock, configure: str, error: str
) -> None:
    account = ACCOUNT_NUMBER
    if configure == "invalid_format":
        account = "not an account!"
    elif configure == "not_found":
        mock_client.async_get_property_ids.return_value = None
    elif configure == "auth":
        mock_client.async_get_property_ids.side_effect = OctopusChargeAuthError("x")
    elif configure == "server":
        mock_client.async_get_property_ids.side_effect = OctopusChargeServerError("x")
    elif configure == "connection":
        mock_client.async_get_property_ids.side_effect = OctopusChargeConnectionError(
            "x"
        )
    elif configure == "unexpected":
        mock_client.async_get_property_ids.side_effect = RuntimeError("x")
    elif configure == "no_charger":
        mock_client.async_get_charge_points.return_value = []
    elif configure == "no_properties":
        mock_client.async_get_property_ids.return_value = []

    result = await _start(hass)
    result = await _submit(
        hass, result["flow_id"], api_key=API_KEY, account_number=account
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    # Fixing the problem lets the same flow carry on to success.
    mock_client.async_get_property_ids.side_effect = None
    mock_client.async_get_property_ids.return_value = [PROPERTY_ID]
    mock_client.async_get_charge_points.return_value = [charge_point()]
    result = await _submit(
        hass, result["flow_id"], api_key=API_KEY, account_number=ACCOUNT_NUMBER
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_second_property_with_a_charger_is_enough(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    mock_client.async_get_property_ids.return_value = ["1", PROPERTY_ID]
    mock_client.async_get_charge_points.side_effect = [[], [charge_point()]]

    result = await _start(hass)
    result = await _submit(
        hass, result["flow_id"], api_key=API_KEY, account_number=ACCOUNT_NUMBER
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_account_can_only_be_added_once(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    mock_config_entry.add_to_hass(hass)

    result = await _start(hass)
    result = await _submit(
        hass, result["flow_id"], api_key=API_KEY, account_number=ACCOUNT_NUMBER
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_client: AsyncMock
) -> None:
    result = await setup_integration.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"][CONF_ACCOUNT_NUMBER] == ACCOUNT_NUMBER

    mock_client.async_get_property_ids.side_effect = OctopusChargeAuthError("x")
    result = await _submit(hass, result["flow_id"], api_key="wrong")
    assert result["errors"] == {"base": "invalid_auth"}

    mock_client.async_get_property_ids.side_effect = None
    result = await _submit(hass, result["flow_id"], api_key="sk_live_new")
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert setup_integration.data[CONF_API_KEY] == "sk_live_new"
    assert setup_integration.data[CONF_ACCOUNT_NUMBER] == ACCOUNT_NUMBER
