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


def _octopus_energy_entry(
    hass: HomeAssistant, account: str = ACCOUNT_NUMBER, key: str = "sk_live_oe"
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain="octopus_energy", data={"api_key": key, "account_id": account}
    )
    entry.add_to_hass(hass)
    return entry


async def test_menu_offers_the_octopus_energy_api_key(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    _octopus_energy_entry(hass)

    result = await _start(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["existing", "manual"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "existing"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "existing"

    result = await _submit(hass, result["flow_id"], account_number=ACCOUNT_NUMBER)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_API_KEY: "sk_live_oe",
        CONF_ACCOUNT_NUMBER: ACCOUNT_NUMBER,
    }


async def test_menu_manual_option_shows_the_normal_form(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    _octopus_energy_entry(hass)

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "manual"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await _submit(
        hass, result["flow_id"], api_key="typed", account_number=ACCOUNT_NUMBER
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_API_KEY] == "typed"


async def test_existing_key_that_does_not_work_reports_the_error(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    _octopus_energy_entry(hass)
    mock_client.async_get_property_ids.side_effect = OctopusChargeAuthError("x")

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "existing"}
    )
    result = await _submit(hass, result["flow_id"], account_number=ACCOUNT_NUMBER)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "existing"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_only_accounts_not_already_set_up_are_offered(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, mock_client: AsyncMock
) -> None:
    mock_config_entry.add_to_hass(hass)
    _octopus_energy_entry(hass)  # same account as the one we already have
    _octopus_energy_entry(hass, account="A-87654321", key="other")

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "existing"}
    )

    schema = result["data_schema"].schema
    account_field = next(iter(schema))
    assert list(schema[account_field].container) == ["A-87654321"]


async def test_no_menu_without_a_usable_octopus_energy_entry(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    MockConfigEntry(domain="octopus_energy", data={"api_key": 1}).add_to_hass(hass)
    MockConfigEntry(domain="octopus_energy", data={}).add_to_hass(hass)

    result = await _start(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_existing_account_set_up_meanwhile_aborts(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """The chosen account was set up by another flow after the form was shown."""
    _octopus_energy_entry(hass)
    _octopus_energy_entry(hass, account="A-87654321", key="other")

    first = await _start(hass)
    first = await hass.config_entries.flow.async_configure(
        first["flow_id"], {"next_step_id": "existing"}
    )
    second = await _start(hass)
    second = await hass.config_entries.flow.async_configure(
        second["flow_id"], {"next_step_id": "existing"}
    )

    result = await _submit(hass, first["flow_id"], account_number=ACCOUNT_NUMBER)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    result = await _submit(hass, second["flow_id"], account_number=ACCOUNT_NUMBER)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_only_existing_account_set_up_meanwhile_aborts(
    hass: HomeAssistant, mock_client: AsyncMock
) -> None:
    """Also when it was the only account offered (nothing left to offer)."""
    _octopus_energy_entry(hass)

    first = await _start(hass)
    first = await hass.config_entries.flow.async_configure(
        first["flow_id"], {"next_step_id": "existing"}
    )
    second = await _start(hass)
    second = await hass.config_entries.flow.async_configure(
        second["flow_id"], {"next_step_id": "existing"}
    )

    result = await _submit(hass, first["flow_id"], account_number=ACCOUNT_NUMBER)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    result = await _submit(hass, second["flow_id"], account_number=ACCOUNT_NUMBER)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
