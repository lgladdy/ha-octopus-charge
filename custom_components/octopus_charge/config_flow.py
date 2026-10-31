"""Config flow for the Octopus Charge integration."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_API_KEY
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api.client import OctopusChargeApiClient
from .api.exceptions import OctopusChargeAuthError, OctopusChargeError
from .const import CONF_ACCOUNT_NUMBER, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Octopus account numbers look like "A-1234ABCD".
ACCOUNT_NUMBER_PATTERN = re.compile(r"^[A-Z0-9-]{3,20}$")

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): str,
        vol.Required(CONF_ACCOUNT_NUMBER): str,
    }
)
STEP_REAUTH_DATA_SCHEMA = vol.Schema({vol.Required(CONF_API_KEY): str})

# The Octopus Energy integration's config entry data keys.
OCTOPUS_ENERGY_DOMAIN = "octopus_energy"
OCTOPUS_ENERGY_API_KEY = "api_key"
OCTOPUS_ENERGY_ACCOUNT_ID = "account_id"

API_KEY_URL = (
    "https://octopus.energy/dashboard/new/accounts/personal-details/api-access"
)


class OctopusChargeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Octopus Charge."""

    VERSION = 1

    async def _async_validate(self, api_key: str, account_number: str) -> str | None:
        """Check the credentials work and the account has a charger.

        Returns an error key, or None if everything is fine.
        """
        if not ACCOUNT_NUMBER_PATTERN.match(account_number):
            return "invalid_account_number"

        client = OctopusChargeApiClient(async_get_clientsession(self.hass), api_key)
        try:
            property_ids = await client.async_get_property_ids(account_number)
            if property_ids is None:
                return "account_not_found"

            for property_id in property_ids:
                if await client.async_get_charge_points(account_number, property_id):
                    return None
        except OctopusChargeAuthError:
            return "invalid_auth"
        except OctopusChargeError as err:
            # No traceback: the chained aiohttp error can contain the URL.
            _LOGGER.warning("Error validating Octopus credentials: %s", err)
            return "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected exception")
            return "unknown"

        return "no_charge_point"

    def _existing_accounts(self) -> dict[str, str]:
        """Return account number -> API key for the Octopus Energy integration.

        Only accounts that aren't already set up here are offered.
        """
        configured = self._async_current_ids()
        accounts: dict[str, str] = {}
        for entry in self.hass.config_entries.async_entries(OCTOPUS_ENERGY_DOMAIN):
            api_key = entry.data.get(OCTOPUS_ENERGY_API_KEY)
            account_number = entry.data.get(OCTOPUS_ENERGY_ACCOUNT_ID)
            if (
                isinstance(api_key, str)
                and isinstance(account_number, str)
                and account_number.strip().upper() not in configured
            ):
                accounts[account_number.strip().upper()] = api_key.strip()

        return accounts

    async def _async_try_create_entry(
        self, api_key: str, account_number: str
    ) -> tuple[ConfigFlowResult | None, str | None]:
        """Validate the details and create the entry, or return the error key."""
        await self.async_set_unique_id(account_number)
        self._abort_if_unique_id_configured()

        if (error := await self._async_validate(api_key, account_number)) is not None:
            return None, error

        return (
            self.async_create_entry(
                title=account_number,
                data={CONF_API_KEY: api_key, CONF_ACCOUNT_NUMBER: account_number},
            ),
            None,
        )

    def _async_show_user_form(
        self, user_input: dict[str, Any] | None, errors: dict[str, str]
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_DATA_SCHEMA, user_input
            ),
            description_placeholders={"api_key_url": API_KEY_URL},
            errors=errors,
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step.

        If the Octopus Energy integration is set up, first offer to reuse its API
        key rather than making the user find and paste it again.
        """
        if user_input is None and self._existing_accounts():
            return self.async_show_menu(
                step_id="user",
                menu_options=["existing", "manual"],
                description_placeholders={"api_key_url": API_KEY_URL},
            )

        errors: dict[str, str] = {}
        if user_input is not None:
            result, error = await self._async_try_create_entry(
                user_input[CONF_API_KEY].strip(),
                user_input[CONF_ACCOUNT_NUMBER].strip().upper(),
            )
            if result is not None:
                return result

            errors["base"] = error or "unknown"

        return self._async_show_user_form(user_input, errors)

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Enter an API key by hand (chosen from the menu)."""
        return self._async_show_user_form(None, {})

    async def async_step_existing(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reuse the API key from an Octopus Energy integration account."""
        accounts = self._existing_accounts()
        if not accounts:
            return await self.async_step_manual()

        errors: dict[str, str] = {}
        if user_input is not None:
            account_number = user_input[CONF_ACCOUNT_NUMBER]
            result, error = await self._async_try_create_entry(
                accounts[account_number], account_number
            )
            if result is not None:
                return result

            errors["base"] = error or "unknown"

        return self.async_show_form(
            step_id="existing",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ACCOUNT_NUMBER, default=next(iter(accounts))
                    ): vol.In(list(accounts))
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle the API key being rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new API key."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            error = await self._async_validate(api_key, entry.data[CONF_ACCOUNT_NUMBER])
            if error is None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: api_key}
                )

            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_DATA_SCHEMA,
            description_placeholders={
                CONF_ACCOUNT_NUMBER: entry.data[CONF_ACCOUNT_NUMBER]
            },
            errors=errors,
        )
