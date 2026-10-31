"""Client for the Octopus Energy APIs used by Octopus Charge."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn

import aiohttp
from homeassistant.util import dt as dt_util
from pydantic import ValidationError

from .exceptions import (
    OctopusChargeAuthError,
    OctopusChargeConnectionError,
    OctopusChargeError,
    OctopusChargeRequestError,
    OctopusChargeServerError,
    OctopusChargeUnauthenticatedError,
)
from .models import (
    ChargePointDaySchedule,
    ChargePointPowerReading,
    OnboardedChargePoint,
)
from .stream import parse_power_stream_chunk

_LOGGER = logging.getLogger(__name__)

KRAKEN_GRAPHQL_URL = "https://api.oegb-kraken.energy/v1/graphql/"
BACKEND_GRAPHQL_URL = "https://api.backend.octopus.energy/v1/graphql/"
CONTEXT_HEADER = "Ha-Integration-Context"

# Octopus rate limits token retrieval per account. A failed attempt does not
# update our token expiry, so without a cooldown every subsequent request would
# attempt to retrieve a new token, which keeps the rate limit tripped.
MINIMUM_TOKEN_RETRIEVAL_COOLDOWN = timedelta(minutes=1)
MAXIMUM_TOKEN_RETRIEVAL_COOLDOWN = timedelta(minutes=30)

# Used when a token's expiry can't be decoded.
FALLBACK_TOKEN_LIFETIME = timedelta(minutes=55)
TOKEN_EXPIRY_MARGIN = timedelta(minutes=5)

CONTROL_MODES = ("SMART", "MANUAL")

AUTH_ERROR_CODES = (
    "KT-CT-1139",
    "KT-CT-1111",
    "KT-CT-1143",
    "KT-CT-1134",
    "KT-CT-1135",
    "OE-0103",
)

# String values are always interpolated through _quote (JSON string syntax is a
# valid GraphQL string), and enum/boolean/number values are validated, so a
# crafted value can't alter the shape of a query.
TOKEN_MUTATION = """mutation {{
  obtainKrakenToken(input: {{ {credential} }}) {{
    token
    refreshToken
    refreshExpiresIn
  }}
}}"""

ACCOUNT_QUERY = """query {{
  account(accountNumber: {account}) {{
    __typename
  }}
  properties(accountNumber: {account}) {{
    id
    occupancyPeriods {{
      effectiveTo
    }}
  }}
}}"""

CHARGE_POINTS_QUERY = """query {{
  chargePointsAtLocation(accountNumber: {account}, propertyId: {property}) {{
    chargePoints {{
      deviceUUID model serialNumber bluetoothLowEnergyPin simcardIdentifier
      firmwareVersion controlMode chargingMethod operationalState boostEndTime
      onboarding {{ accountNumber propertyId onboardedAt externalDeviceId }}
      configuration {{
        isRandomDelayEnabled isConnected LEDBrightnessPercentage
        isChargeCableAutoLockAvailable isChargeCableAutoLockEnabled
        isEcoModeEnabled isAwayMode
      }}
    }}
  }}
}}"""

SCHEDULES_QUERY = """query {{
  chargePointSchedules(accountNumber: {account}, deviceUUID: {device}) {{
    schedules {{
      day
      chargePointScheduleSettings {{
        start
        end
        action
      }}
    }}
  }}
}}"""

# mutation name, input field name -> template. All mutations select __typename only.
SET_ECO_MODE_MUTATION = """mutation {{
  chargePointSetECOModeState(input: {{
    accountNumber: {account},
    deviceUUID: {device},
    enableECOMode: {value}
  }}) {{ __typename }}
}}"""

SET_RANDOM_DELAY_MUTATION = """mutation {{
  chargePointSetRandomDelay(input: {{
    accountNumber: {account},
    deviceUUID: {device},
    enableRandomDelay: {value}
  }}) {{ __typename }}
}}"""

SET_CABLE_AUTO_LOCK_MUTATION = """mutation {{
  chargePointSetChargeCableAutoLockState(input: {{
    accountNumber: {account},
    deviceUUID: {device},
    enableChargeCableAutoLock: {value}
  }}) {{ __typename }}
}}"""

SET_AWAY_MODE_MUTATION = """mutation {{
  chargePointSetAwayModeState(input: {{
    accountNumber: {account},
    deviceUUID: {device},
    enableAwayMode: {value}
  }}) {{ __typename }}
}}"""

SET_CONTROL_MODE_MUTATION = """mutation {{
  chargePointSetControlMode(input: {{
    accountNumber: {account},
    deviceUUID: {device},
    controlMode: {value}
  }}) {{ __typename }}
}}"""

SET_LED_BRIGHTNESS_MUTATION = """mutation {{
  chargePointSetLEDBrightness(input: {{
    accountNumber: {account},
    deviceUUID: {device},
    brightnessPercentage: {value}
  }}) {{ __typename }}
}}"""

START_BOOST_MUTATION = """mutation {{
  chargePointStartBoostCharge(input: {{
    accountNumber: {account},
    deviceUUID: {device},
    endTime: {end_time}
  }}) {{ __typename }}
}}"""

STOP_BOOST_MUTATION = """mutation {{
  chargePointStopBoostCharge(input: {{
    accountNumber: {account},
    deviceUUID: {device}
  }}) {{ __typename }}
}}"""

POWER_READINGS_SUBSCRIPTION = """subscription OctoChargerGetPowerReadings(
  $accountNumber: String!, $deviceUUID: UUID!
) {
  electricChargerPowerReadings(
    accountNumber: $accountNumber, deviceUUID: $deviceUUID
  ) {
    __typename
    value
    unit
  }
}"""


def _quote(value: str) -> str:
    """Return a value as a GraphQL string literal."""
    return json.dumps(value)


def _bool(value: bool) -> str:
    """Return a bool as a GraphQL boolean literal."""
    return "true" if value else "false"


def _excerpt(text: str, limit: int = 200) -> str:
    """Return the start of a response body for an error message."""
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[:limit]}..."


def calculate_token_retrieval_cooldown(failure_count: int) -> timedelta:
    """Return how long to wait before retrieving a token after repeated failures."""
    if failure_count < 1:
        return timedelta(0)

    # Clamp the exponent: the cap is reached after a handful of failures, but
    # the count keeps growing for as long as Octopus stays down.
    exponent = min(failure_count - 1, 16)
    cooldown = MINIMUM_TOKEN_RETRIEVAL_COOLDOWN * (2**exponent)
    return min(cooldown, MAXIMUM_TOKEN_RETRIEVAL_COOLDOWN)


def _decode_jwt_expiry(token: str) -> datetime:
    """Decode the expiry of a JWT, falling back to a default lifetime."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return datetime.fromtimestamp(claims["exp"], tz=UTC)
    except IndexError, KeyError, ValueError, TypeError:
        _LOGGER.debug("Failed to decode token expiry - using default", exc_info=True)
        return dt_util.utcnow() + FALLBACK_TOKEN_LIFETIME


def _select_current_property_ids(properties: list[dict[str, Any]]) -> list[str]:
    """Return the ids of properties the account currently occupies.

    A property is current if any of its occupancy periods has no end date or
    ends in the future. If that excludes every property (e.g. the API shape
    changed) fall back to all of them rather than ending up with none.
    """
    now = dt_util.utcnow()
    current: list[str] = []
    everything: list[str] = []
    for prop in properties:
        property_id = prop.get("id")
        if property_id is None:
            continue
        if property_id not in everything:
            everything.append(property_id)

        for period in prop.get("occupancyPeriods") or []:
            effective_to = period.get("effectiveTo")
            parsed = dt_util.parse_datetime(effective_to) if effective_to else None
            # An offset-less value can't be compared with an aware "now"; Kraken
            # works in UTC, so treat it as UTC.
            if parsed is not None and parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            ends = parsed
            if (effective_to is None or ends is None or ends > now) and (
                property_id not in current
            ):
                current.append(property_id)

    return current or everything


class OctopusChargeApiClient:
    """Talks to the Octopus Energy APIs for a single account."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_key: str,
        user_agent: str = "ha-octopus-charge",
        timeout_in_seconds: int = 20,
    ) -> None:
        """Initialise the client."""
        self._session = session
        self._api_key = api_key
        self._headers = {"user-agent": user_agent}
        self._timeout = aiohttp.ClientTimeout(
            total=None, sock_connect=timeout_in_seconds, sock_read=timeout_in_seconds
        )

        self._token: str | None = None
        self._token_expiry: datetime | None = None
        self._refresh_token: str | None = None
        self._refresh_token_expiry: datetime | None = None
        self._token_lock = asyncio.Lock()
        self._is_api_key_invalid = False
        self._token_retrieval_failures = 0
        self._token_retrieval_cooldown_until: datetime | None = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    def _is_token_valid(self) -> bool:
        return (
            self._token is not None
            and self._token_expiry is not None
            and self._token_expiry - TOKEN_EXPIRY_MARGIN > dt_util.utcnow()
        )

    def _reset_tokens(self) -> None:
        self._token = None
        self._token_expiry = None
        self._refresh_token = None
        self._refresh_token_expiry = None

    async def _async_ensure_token(self) -> tuple[str, bool]:
        """Return a valid auth token, retrieving one if needed.

        Also returns whether the token was retrieved by this call.
        """
        if self._is_token_valid():
            return self._token, False  # type: ignore[return-value]

        async with self._token_lock:
            if self._is_token_valid():
                return self._token, False  # type: ignore[return-value]

            if self._is_api_key_invalid:
                raise OctopusChargeAuthError("API key has been marked as invalid")

            now = dt_util.utcnow()
            if (
                self._token_retrieval_cooldown_until is not None
                and self._token_retrieval_cooldown_until > now
            ):
                raise OctopusChargeServerError(
                    "Token retrieval is in cooldown until "
                    f"{self._token_retrieval_cooldown_until} after "
                    f"{self._token_retrieval_failures} consecutive failure(s)"
                )

            if (
                self._refresh_token_expiry is not None
                and self._refresh_token_expiry < now
            ):
                self._refresh_token = None

            try:
                await self._async_retrieve_token()
            except OctopusChargeAuthError:
                self._is_api_key_invalid = True
                raise
            except OctopusChargeConnectionError:
                # Never reached Octopus, so it doesn't count towards its rate
                # limit; the callers' own retry backoff paces the next attempt.
                raise
            except OctopusChargeError:
                # Any other failure (server error, rate limit reported as a
                # GraphQL error...) gets the cooldown, so a struggling or rate
                # limiting token endpoint isn't hit again on every request.
                self._record_token_retrieval_failure()
                raise

            # The failure count is only reset once a request is accepted with the
            # new token (see _token_accepted), so tokens that are retrieved but
            # then refused still back off further each time.
            self._token_retrieval_cooldown_until = None
            return self._token, True  # type: ignore[return-value]

    async def _async_retrieve_token(self) -> None:
        """Retrieve a token, preferring the refresh token over the API key.

        Only the API key being refused means the key itself is bad: a refused
        refresh token (expired, revoked...) falls back to the API key.
        """
        if self._refresh_token is not None:
            try:
                await self._async_fetch_token()
            except OctopusChargeAuthError:
                _LOGGER.debug(
                    "Refreshing with the refresh token failed - trying the API key"
                )
                self._reset_tokens()
            else:
                return

        await self._async_fetch_token()

    def _record_token_retrieval_failure(self) -> None:
        """Count a failed token retrieval and start (or lengthen) the cooldown."""
        self._token_retrieval_failures += 1
        self._token_retrieval_cooldown_until = (
            dt_util.utcnow()
            + calculate_token_retrieval_cooldown(self._token_retrieval_failures)
        )
        _LOGGER.debug(
            "Failed to retrieve a usable token %s time(s) in a row - not trying "
            "again until %s",
            self._token_retrieval_failures,
            self._token_retrieval_cooldown_until,
        )

    def _token_rejected(self, token: str, *, fresh: bool) -> None:
        """Handle Octopus refusing a request's token.

        A token that had been in use is thrown away so the next attempt retrieves
        a new one with the API key - but only if it is still the current one, so
        a late rejection can't discard a token another request just retrieved. A
        token retrieved for this very request being refused too is something
        retrieving yet another one won't fix, so it counts towards the cooldown
        instead of spending the rate limited token endpoint on every request.
        """
        if fresh:
            self._record_token_retrieval_failure()
        elif self._token == token:
            self._reset_tokens()

    def _token_accepted(self) -> None:
        """Note that a request was accepted, ending any run of token failures."""
        self._token_retrieval_failures = 0

    async def _async_fetch_token(self) -> None:
        if self._refresh_token is None:
            credential = f"APIKey: {_quote(self._api_key)}"
        else:
            credential = f"refreshToken: {_quote(self._refresh_token)}"

        body = await self._async_post(
            KRAKEN_GRAPHQL_URL,
            {"query": TOKEN_MUTATION.format(credential=credential)},
            "refresh-token",
        )
        token = (body.get("data") or {}).get("obtainKrakenToken")
        try:
            access_token = token["token"]
            refresh_token = token["refreshToken"]
            refresh_expiry = datetime.fromtimestamp(token["refreshExpiresIn"], tz=UTC)
        except (KeyError, TypeError, ValueError, OverflowError, OSError) as err:
            # A malformed response says nothing about whether the key is valid -
            # Octopus reports a rejected key as a GraphQL error.
            raise OctopusChargeRequestError(
                "Unexpected response when retrieving an auth token"
            ) from err
        if not isinstance(access_token, str) or not isinstance(refresh_token, str):
            raise OctopusChargeRequestError(
                "Unexpected response when retrieving an auth token"
            )

        self._token = access_token
        self._refresh_token = refresh_token
        self._refresh_token_expiry = refresh_expiry
        self._token_expiry = _decode_jwt_expiry(access_token)

    # ------------------------------------------------------------------
    # Request plumbing
    # ------------------------------------------------------------------
    async def _async_post(
        self,
        url: str,
        payload: dict[str, Any],
        context: str,
        token: str | None = None,
    ) -> dict[str, Any]:
        """POST a GraphQL payload and return the decoded, error-checked response."""
        headers = {CONTEXT_HEADER: context, **self._headers}
        if token is not None:
            headers["Authorization"] = token

        try:
            async with self._session.post(
                url, json=payload, headers=headers, timeout=self._timeout
            ) as response:
                text = await response.text()
                status = response.status
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning("Failed to connect (%s): %s", context, type(err).__name__)
            raise OctopusChargeConnectionError(
                f"Failed to connect ({context})"
            ) from err

        return self._process_response(status, text, context)

    def _raise_for_status(self, status: int, text: str, context: str) -> NoReturn:
        """Raise the error for an HTTP error status (400 and above)."""
        if status >= 500 or status == 429:
            raise OctopusChargeServerError(
                f"Octopus server error ({context}): {status}; {_excerpt(text)}"
            )

        if status in (401, 403):
            # Cloudfront errors are returned as 403s, so check the body to tell
            # them apart from an unauthenticated request.
            if status == 403 and "The request could not be satisfied" in text:
                raise OctopusChargeServerError(
                    f"Octopus server error ({context}): {status}"
                )

            raise OctopusChargeUnauthenticatedError(
                f"Unauthenticated request ({context}): {status}"
            )

        raise OctopusChargeRequestError(
            f"Request failed ({context}): {status}; {_excerpt(text)}"
        )

    def _process_response(self, status: int, text: str, context: str) -> dict[str, Any]:
        if status >= 400:
            self._raise_for_status(status, text, context)

        try:
            data = json.loads(text)
        except ValueError as err:
            raise OctopusChargeRequestError(
                f"Failed to decode response ({context})"
            ) from err

        if not isinstance(data, dict):
            raise OctopusChargeRequestError(f"Unexpected response ({context})")

        errors = data.get("errors")
        if errors:
            messages = [str(error.get("message", "")).strip(".,!") for error in errors]
            for error in errors:
                code = (error.get("extensions") or {}).get("errorCode")
                if code in AUTH_ERROR_CODES:
                    raise OctopusChargeAuthError(
                        f"Authentication failed ({context}) - {', '.join(messages)}",
                        messages,
                    )

            raise OctopusChargeRequestError(
                f"Request failed ({context}) - {', '.join(messages)}", messages
            )

        return data

    async def _async_graphql(
        self, query: str, context: str, url: str = BACKEND_GRAPHQL_URL
    ) -> dict[str, Any]:
        """Run an authenticated GraphQL operation.

        If a token that has been in use is refused - with an HTTP 401/403 or an
        auth error code - retry once with a new one retrieved using the API key,
        since tokens can be revoked or expire early. If a freshly retrieved token
        is refused too:
        - with an auth error code, the key itself lacks access, so it is raised
          as an auth error (prompting reauth);
        - with an HTTP 401/403, re-entering the key won't fix it, so it is a
          request error (and counts towards the token retrieval cooldown).
        """
        token, fresh = await self._async_ensure_token()
        while True:
            try:
                body = await self._async_post(url, {"query": query}, context, token)
                break
            except OctopusChargeAuthError as err:
                if not fresh:
                    self._token_rejected(token, fresh=False)
                elif isinstance(err, OctopusChargeUnauthenticatedError):
                    self._token_rejected(token, fresh=True)
                    raise OctopusChargeRequestError(
                        f"Request rejected with a newly retrieved token ({context})"
                    ) from err
                else:
                    raise

            _LOGGER.debug("Token rejected (%s) - retrying with a new one", context)
            token, _ = await self._async_ensure_token()
            fresh = True

        self._token_accepted()
        return body.get("data") or {}

    # ------------------------------------------------------------------
    # Account / discovery
    # ------------------------------------------------------------------
    async def async_get_property_ids(self, account_number: str) -> list[str] | None:
        """Return the account's current property ids, or None if it doesn't exist."""
        data = await self._async_graphql(
            ACCOUNT_QUERY.format(account=_quote(account_number)),
            "get-account",
            KRAKEN_GRAPHQL_URL,
        )
        if data.get("account") is None:
            return None

        return _select_current_property_ids(data.get("properties") or [])

    async def async_get_charge_points(
        self, account_number: str, property_id: str
    ) -> list[OnboardedChargePoint]:
        """Return the charge points (with status and configuration) at a property."""
        data = await self._async_graphql(
            CHARGE_POINTS_QUERY.format(
                account=_quote(account_number), property=_quote(property_id)
            ),
            "chargepoint-configuration",
        )
        at_location = data.get("chargePointsAtLocation") or {}
        try:
            return [
                OnboardedChargePoint.model_validate(charge_point)
                for charge_point in at_location.get("chargePoints") or []
            ]
        except ValidationError as err:
            raise OctopusChargeRequestError(
                "Unexpected charge point data from Octopus"
            ) from err

    async def async_get_schedules(
        self, account_number: str, device_uuid: str
    ) -> list[ChargePointDaySchedule] | None:
        """Return a charge point's weekly schedule, or None if unavailable."""
        data = await self._async_graphql(
            SCHEDULES_QUERY.format(
                account=_quote(account_number), device=_quote(device_uuid)
            ),
            "chargepoint-schedules",
        )
        schedules = data.get("chargePointSchedules")
        if schedules is None:
            return None

        try:
            return [
                ChargePointDaySchedule.model_validate(day)
                for day in schedules.get("schedules") or []
            ]
        except ValidationError as err:
            raise OctopusChargeRequestError(
                "Unexpected charge point schedule from Octopus"
            ) from err

    # ------------------------------------------------------------------
    # Controls
    # ------------------------------------------------------------------
    async def _async_mutate(
        self, template: str, context: str, account_number: str, device_uuid: str, **kw
    ) -> None:
        await self._async_graphql(
            template.format(
                account=_quote(account_number), device=_quote(device_uuid), **kw
            ),
            context,
        )

    async def async_set_eco_mode(
        self, account_number: str, device_uuid: str, enabled: bool
    ) -> None:
        """Turn eco mode on or off."""
        await self._async_mutate(
            SET_ECO_MODE_MUTATION,
            "set-chargepoint-eco-mode",
            account_number,
            device_uuid,
            value=_bool(enabled),
        )

    async def async_set_random_delay(
        self, account_number: str, device_uuid: str, enabled: bool
    ) -> None:
        """Turn random delay on or off."""
        await self._async_mutate(
            SET_RANDOM_DELAY_MUTATION,
            "set-chargepoint-random-delay",
            account_number,
            device_uuid,
            value=_bool(enabled),
        )

    async def async_set_cable_auto_lock(
        self, account_number: str, device_uuid: str, enabled: bool
    ) -> None:
        """Turn the charge cable auto lock on or off."""
        await self._async_mutate(
            SET_CABLE_AUTO_LOCK_MUTATION,
            "set-chargepoint-cable-auto-lock",
            account_number,
            device_uuid,
            value=_bool(enabled),
        )

    async def async_set_away_mode(
        self, account_number: str, device_uuid: str, enabled: bool
    ) -> None:
        """Turn away mode on or off."""
        await self._async_mutate(
            SET_AWAY_MODE_MUTATION,
            "set-chargepoint-away-mode",
            account_number,
            device_uuid,
            value=_bool(enabled),
        )

    async def async_set_control_mode(
        self, account_number: str, device_uuid: str, control_mode: str
    ) -> None:
        """Set the control mode (SMART or MANUAL)."""
        if control_mode not in CONTROL_MODES:
            raise ValueError(f"Unsupported control mode: {control_mode}")

        await self._async_mutate(
            SET_CONTROL_MODE_MUTATION,
            "set-chargepoint-control-mode",
            account_number,
            device_uuid,
            value=control_mode,
        )

    async def async_set_led_brightness(
        self, account_number: str, device_uuid: str, brightness_percentage: int
    ) -> None:
        """Set the LED brightness (0-100)."""
        await self._async_mutate(
            SET_LED_BRIGHTNESS_MUTATION,
            "set-chargepoint-led-brightness",
            account_number,
            device_uuid,
            value=int(brightness_percentage),
        )

    async def async_start_boost(
        self, account_number: str, device_uuid: str, end_datetime: datetime
    ) -> None:
        """Start a boost charge that ends at the given time."""
        await self._async_mutate(
            START_BOOST_MUTATION,
            "start-chargepoint-boost",
            account_number,
            device_uuid,
            end_time=_quote(end_datetime.isoformat(sep="T")),
        )

    async def async_stop_boost(self, account_number: str, device_uuid: str) -> None:
        """Stop a boost charge."""
        await self._async_mutate(
            STOP_BOOST_MUTATION,
            "stop-chargepoint-boost",
            account_number,
            device_uuid,
        )

    # ------------------------------------------------------------------
    # Live power
    # ------------------------------------------------------------------
    async def async_stream_power(
        self, account_number: str, device_uuid: str
    ) -> AsyncIterator[ChargePointPowerReading | None]:
        """Stream live power readings for a charge point.

        Despite the schema naming this ``electricChargerPowerReadings`` under
        ``Subscription``, it is not served over a WebSocket - it is a single
        long-lived HTTP POST whose response streams as
        ``multipart/mixed;boundary=graphql``, one chunk per reading (or an empty
        ``{}`` heartbeat between them), for as long as the connection stays open.
        The only thing that makes the server stream instead of resolving once and
        closing is the Accept header's ``subscriptionSpec=1.0`` parameter.

        Yields a ChargePointPowerReading for each reading, or None when the stream
        reports no reading for that update (e.g. charging just stopped).
        """
        payload = {
            "query": POWER_READINGS_SUBSCRIPTION,
            "operationName": "OctoChargerGetPowerReadings",
            "variables": {"accountNumber": account_number, "deviceUUID": device_uuid},
            "extensions": {
                "clientLibrary": {"name": "apollo-kotlin", "version": "4.3.3"}
            },
        }

        context = "chargepoint-power-stream"
        token, _ = await self._async_ensure_token()
        headers = {
            "Authorization": token,
            "Accept": (
                "multipart/mixed;subscriptionSpec=1.0, "
                "application/graphql-response+json, application/json"
            ),
            "X-APOLLO-OPERATION-NAME": "OctoChargerGetPowerReadings",
            CONTEXT_HEADER: context,
            **self._headers,
        }

        try:
            async with self._session.post(
                BACKEND_GRAPHQL_URL,
                json=payload,
                headers=headers,
                timeout=self._timeout,
            ) as response:
                if response.status >= 400:
                    text = await response.text()
                    try:
                        self._raise_for_status(response.status, text, context)
                    except OctopusChargeUnauthenticatedError as err:
                        # Unlike other requests, a refused stream doesn't discard
                        # or replace the shared token: the status polls are still
                        # using it, and the stream reconnects every minute or so
                        # while charging, so it would churn the rate limited token
                        # endpoint. If the token really is bad, the next poll
                        # replaces it and the next reconnect picks that up.
                        raise OctopusChargeRequestError(
                            "Live power stream refused the token"
                        ) from err

                self._token_accepted()
                buffer = ""
                async for chunk in response.content.iter_any():
                    buffer, readings = parse_power_stream_chunk(buffer, chunk)
                    for reading in readings:
                        yield reading
        except (TimeoutError, aiohttp.ClientError) as err:
            raise OctopusChargeConnectionError(
                "Connection lost while streaming charge point power"
            ) from err
