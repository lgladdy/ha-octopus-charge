"""Tests for the Octopus API client."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.octopus_charge.api.client import (
    BACKEND_GRAPHQL_URL,
    KRAKEN_GRAPHQL_URL,
    MAXIMUM_TOKEN_RETRIEVAL_COOLDOWN,
    OctopusChargeApiClient,
    _select_current_property_ids,
    calculate_token_retrieval_cooldown,
)
from custom_components.octopus_charge.api.exceptions import (
    OctopusChargeAuthError,
    OctopusChargeConnectionError,
    OctopusChargeError,
    OctopusChargeRequestError,
    OctopusChargeServerError,
)

from ..conftest import (
    ACCOUNT_NUMBER,
    API_KEY,
    DEVICE_UUID,
    PROPERTY_ID,
    charge_point_data,
)


def make_jwt(expires_in: timedelta) -> str:
    """Build an unsigned JWT-shaped token expiring ``expires_in`` from now."""

    def _encode(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    expiry = int((dt_util.utcnow() + expires_in).timestamp())
    return f"{_encode({'alg': 'none'})}.{_encode({'exp': expiry})}.sig"


def token_response(expires_in: timedelta = timedelta(hours=1)) -> dict[str, Any]:
    """Return a successful obtainKrakenToken response."""
    return {
        "data": {
            "obtainKrakenToken": {
                "token": make_jwt(expires_in),
                "refreshToken": "refresh-token",
                "refreshExpiresIn": int(
                    (dt_util.utcnow() + timedelta(days=7)).timestamp()
                ),
            }
        }
    }


def calls_to(mocker: AiohttpClientMocker, url: str) -> list[Any]:
    return [call for call in mocker.mock_calls if str(call[1]) == url]


@pytest.fixture
async def client(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> OctopusChargeApiClient:
    """Return a client whose session is backed by the request mocker."""
    return OctopusChargeApiClient(async_get_clientsession(hass), API_KEY)


@pytest.fixture
def token_mock(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """Answer token requests successfully."""
    aioclient_mock.post(KRAKEN_GRAPHQL_URL, json=token_response())
    return aioclient_mock


# --- cooldown ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("failures", "minutes"), [(0, 0), (1, 1), (2, 2), (3, 4), (4, 8), (5, 16)]
)
def test_cooldown_doubles_per_failure(failures: int, minutes: int) -> None:
    assert calculate_token_retrieval_cooldown(failures) == timedelta(minutes=minutes)


def test_cooldown_is_capped() -> None:
    assert calculate_token_retrieval_cooldown(50) == MAXIMUM_TOKEN_RETRIEVAL_COOLDOWN


# --- property selection -----------------------------------------------------


def test_selects_only_current_properties() -> None:
    future = (dt_util.utcnow() + timedelta(days=30)).isoformat()
    past = (dt_util.utcnow() - timedelta(days=30)).isoformat()

    assert _select_current_property_ids(
        [
            {"id": "old", "occupancyPeriods": [{"effectiveTo": past}]},
            {"id": "open", "occupancyPeriods": [{"effectiveTo": None}]},
            {"id": "future", "occupancyPeriods": [{"effectiveTo": future}]},
        ]
    ) == ["open", "future"]


def test_offset_less_end_dates_are_treated_as_utc() -> None:
    """A naive date can't be compared with an aware "now" - it mustn't crash."""
    properties = [
        {"id": "old", "occupancyPeriods": [{"effectiveTo": "2000-01-01T00:00:00"}]},
        {"id": "new", "occupancyPeriods": [{"effectiveTo": "2999-01-01T00:00:00"}]},
    ]

    assert _select_current_property_ids(properties) == ["new"]


def test_falls_back_to_all_properties_when_none_look_current() -> None:
    past = (dt_util.utcnow() - timedelta(days=30)).isoformat()

    assert _select_current_property_ids(
        [{"id": "a", "occupancyPeriods": [{"effectiveTo": past}]}, {"id": "a"}]
    ) == ["a"]


def test_properties_are_deduplicated() -> None:
    assert _select_current_property_ids(
        [
            {"id": "a", "occupancyPeriods": [{"effectiveTo": None}]},
            {"id": "a", "occupancyPeriods": [{"effectiveTo": None}]},
        ]
    ) == ["a"]


# --- authentication ---------------------------------------------------------


async def test_token_is_fetched_once_and_reused(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    token_mock.post(BACKEND_GRAPHQL_URL, json={"data": {}})

    await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)
    await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)

    assert len(calls_to(token_mock, KRAKEN_GRAPHQL_URL)) == 1
    assert len(calls_to(token_mock, BACKEND_GRAPHQL_URL)) == 2


async def test_token_is_sent_raw_in_authorization_header(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    token_mock.post(BACKEND_GRAPHQL_URL, json={"data": {}})

    await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)

    headers = calls_to(token_mock, BACKEND_GRAPHQL_URL)[0][3]
    assert headers["Authorization"].count(".") == 2
    assert not headers["Authorization"].startswith("JWT")
    assert headers["Ha-Integration-Context"] == "chargepoint-configuration"


async def test_expired_token_is_replaced_using_the_refresh_token(
    client: OctopusChargeApiClient,
    token_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    token_mock.post(BACKEND_GRAPHQL_URL, json={"data": {}})

    await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)
    freezer.tick(timedelta(hours=2))
    await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)

    token_calls = calls_to(token_mock, KRAKEN_GRAPHQL_URL)
    assert len(token_calls) == 2
    assert "APIKey" in token_calls[0][2]["query"]
    assert "refreshToken" in token_calls[1][2]["query"]


async def test_rejected_api_key_is_remembered(
    client: OctopusChargeApiClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(
        KRAKEN_GRAPHQL_URL,
        json={
            "errors": [
                {
                    "message": "Invalid data.",
                    "extensions": {"errorCode": "KT-CT-1139"},
                }
            ]
        },
    )

    with pytest.raises(OctopusChargeAuthError):
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)
    with pytest.raises(OctopusChargeAuthError):
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)

    # The second attempt doesn't hammer Octopus with a key it already rejected.
    assert aioclient_mock.call_count == 1


async def test_token_server_errors_trigger_a_cooldown(
    client: OctopusChargeApiClient,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    aioclient_mock.post(KRAKEN_GRAPHQL_URL, status=429, text="slow down")

    with pytest.raises(OctopusChargeServerError):
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)
    assert aioclient_mock.call_count == 1

    # Within the cooldown no further token request is made.
    with pytest.raises(OctopusChargeServerError):
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)
    assert aioclient_mock.call_count == 1

    # Once it elapses we try again.
    freezer.tick(timedelta(minutes=1, seconds=1))
    with pytest.raises(OctopusChargeServerError):
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)
    assert aioclient_mock.call_count == 2


@pytest.mark.parametrize(
    "response",
    [
        # Kraken reports some failures, rate limiting included, as GraphQL errors.
        {"json": {"errors": [{"message": "Too many requests."}]}},
        {"status": 400, "text": "bad request"},
        {"status": 404, "text": "not found"},
        # A malformed response says nothing about the key being bad.
        {"json": {"data": {"obtainKrakenToken": None}}},
    ],
    ids=["graphql-error", "http-400", "http-404", "malformed"],
)
async def test_other_token_failures_trigger_a_cooldown(
    client: OctopusChargeApiClient,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    response: dict[str, Any],
) -> None:
    aioclient_mock.post(KRAKEN_GRAPHQL_URL, **response)

    with pytest.raises(OctopusChargeRequestError) as exc_info:
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)
    assert not isinstance(exc_info.value, OctopusChargeAuthError)
    assert aioclient_mock.call_count == 1

    # Within the cooldown no further token request is made.
    with pytest.raises(OctopusChargeServerError):
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)
    assert aioclient_mock.call_count == 1


async def test_connection_failures_do_not_trigger_a_cooldown(
    client: OctopusChargeApiClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """A request that was never sent doesn't count towards the rate limit."""
    aioclient_mock.post(
        KRAKEN_GRAPHQL_URL,
        exc=aiohttp.ClientConnectorError(MagicMock(), OSError("refused")),
    )
    with pytest.raises(OctopusChargeConnectionError):
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)

    aioclient_mock.clear_requests()
    aioclient_mock.post(KRAKEN_GRAPHQL_URL, json=token_response())
    aioclient_mock.post(BACKEND_GRAPHQL_URL, json={"data": {}})

    assert await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID) == []


async def test_timeouts_still_trigger_a_cooldown(
    client: OctopusChargeApiClient, aioclient_mock: AiohttpClientMocker
) -> None:
    """Octopus may have received (and counted) a request that timed out."""
    aioclient_mock.post(KRAKEN_GRAPHQL_URL, exc=TimeoutError())
    with pytest.raises(OctopusChargeConnectionError):
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)

    with pytest.raises(OctopusChargeServerError):
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)
    assert aioclient_mock.call_count == 1


async def test_refused_refresh_token_falls_back_to_the_api_key(
    client: OctopusChargeApiClient,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An HTTP 401 for the refresh token mustn't condemn a working API key."""

    async def _token(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        if "APIKey" not in data["query"]:
            return AiohttpClientMockResponse("POST", url, status=401)
        return AiohttpClientMockResponse("POST", url, json=token_response())

    aioclient_mock.post(KRAKEN_GRAPHQL_URL, side_effect=_token)
    aioclient_mock.post(BACKEND_GRAPHQL_URL, json={"data": {}})

    await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)
    freezer.tick(timedelta(hours=2))
    assert await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID) == []

    queries = [
        call[2]["query"] for call in calls_to(aioclient_mock, KRAKEN_GRAPHQL_URL)
    ]
    assert ["APIKey" in q for q in queries] == [True, False, True]


async def test_cooldown_lengthens_with_repeated_failures(
    client: OctopusChargeApiClient,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    aioclient_mock.post(KRAKEN_GRAPHQL_URL, status=500)

    for _ in range(2):
        with pytest.raises(OctopusChargeServerError):
            await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)
        freezer.tick(timedelta(minutes=1, seconds=1))
    assert aioclient_mock.call_count == 2

    # The second failure doubled the cooldown to two minutes, so one minute on
    # we're still waiting.
    with pytest.raises(OctopusChargeServerError):
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)
    assert aioclient_mock.call_count == 2


async def test_successful_token_clears_the_cooldown(
    client: OctopusChargeApiClient,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    aioclient_mock.post(KRAKEN_GRAPHQL_URL, status=500)
    with pytest.raises(OctopusChargeServerError):
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)

    aioclient_mock.clear_requests()
    aioclient_mock.post(KRAKEN_GRAPHQL_URL, json=token_response())
    aioclient_mock.post(BACKEND_GRAPHQL_URL, json={"data": {}})
    freezer.tick(timedelta(minutes=2))

    assert await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID) == []


# --- response handling ------------------------------------------------------


@pytest.mark.parametrize("status", [500, 502, 429])
async def test_server_errors(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker, status: int
) -> None:
    token_mock.post(BACKEND_GRAPHQL_URL, status=status)

    with pytest.raises(OctopusChargeServerError):
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)


async def test_cloudfront_403_is_a_server_error(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    token_mock.post(
        BACKEND_GRAPHQL_URL,
        status=403,
        text="ERROR: The request could not be satisfied",
    )

    with pytest.raises(OctopusChargeServerError):
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)


def backend_sequence(*statuses: int) -> Any:
    """Answer backend requests with these statuses in turn, repeating the last.

    200 answers with empty data. Starting with a 200 lets a test make one
    successful request first, so the client holds a token that has been in use.
    """
    remaining = list(statuses)

    async def _respond(*_: Any) -> AiohttpClientMockResponse:
        status = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if status == 200:
            return AiohttpClientMockResponse(
                "POST", BACKEND_GRAPHQL_URL, json={"data": {}}
            )
        return AiohttpClientMockResponse("POST", BACKEND_GRAPHQL_URL, status=status)

    return _respond


async def test_rejected_token_is_retried_with_a_new_one(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    """A 401 for a token in use retries once with a new one, not a reauth."""
    token_mock.post(BACKEND_GRAPHQL_URL, side_effect=backend_sequence(200, 401, 200))
    await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)

    assert await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID) == []
    # The rejected token was thrown away and a new one retrieved with the key.
    assert len(calls_to(token_mock, KRAKEN_GRAPHQL_URL)) == 2
    assert len(calls_to(token_mock, BACKEND_GRAPHQL_URL)) == 3


async def test_fresh_token_rejected_too_is_not_an_auth_error(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    """If even a brand new token is refused, re-entering the key won't help."""
    token_mock.post(BACKEND_GRAPHQL_URL, side_effect=backend_sequence(200, 401))
    await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)

    with pytest.raises(OctopusChargeRequestError) as exc_info:
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)

    assert not isinstance(exc_info.value, OctopusChargeAuthError)
    assert len(calls_to(token_mock, KRAKEN_GRAPHQL_URL)) == 2
    assert len(calls_to(token_mock, BACKEND_GRAPHQL_URL)) == 3


async def test_fresh_token_rejected_again_backs_off_token_retrieval(
    client: OctopusChargeApiClient,
    token_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A persistent 401 doesn't spend a token retrieval on every request."""
    token_mock.post(BACKEND_GRAPHQL_URL, status=401, text="nope")

    for _ in range(3):
        with pytest.raises(OctopusChargeError):
            await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)
    # The token retrieved for the first request was refused, so the rest wait
    # for the cooldown instead of retrieving more.
    assert len(calls_to(token_mock, KRAKEN_GRAPHQL_URL)) == 1

    freezer.tick(timedelta(minutes=1, seconds=1))
    with pytest.raises(OctopusChargeError):
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)
    assert len(calls_to(token_mock, KRAKEN_GRAPHQL_URL)) == 2

    # Still refused, so the cooldown lengthens rather than restarting at a minute.
    freezer.tick(timedelta(minutes=1, seconds=1))
    with pytest.raises(OctopusChargeServerError):
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)
    assert len(calls_to(token_mock, KRAKEN_GRAPHQL_URL)) == 2


async def test_auth_error_code_for_a_token_in_use_is_retried(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    """A revoked token reported as an auth error code isn't a bad API key."""
    auth_error = {
        "errors": [
            {"message": "Unauthorized.", "extensions": {"errorCode": "KT-CT-1111"}}
        ]
    }
    responses = iter([{"data": {}}, auth_error, {"data": {}}])

    async def _respond(*_: Any) -> AiohttpClientMockResponse:
        return AiohttpClientMockResponse(
            "POST", BACKEND_GRAPHQL_URL, json=next(responses)
        )

    token_mock.post(BACKEND_GRAPHQL_URL, side_effect=_respond)
    await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)

    assert await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID) == []
    assert len(calls_to(token_mock, KRAKEN_GRAPHQL_URL)) == 2


async def test_concurrent_refusals_of_a_new_token_count_once(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    """Requests sharing a never-accepted token don't each fetch a replacement."""
    token_mock.post(BACKEND_GRAPHQL_URL, status=401, text="nope")

    results = await asyncio.gather(
        *(
            client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)
            for _ in range(3)
        ),
        return_exceptions=True,
    )

    assert all(isinstance(result, OctopusChargeError) for result in results)
    assert len(calls_to(token_mock, KRAKEN_GRAPHQL_URL)) == 1
    assert client._token_retrieval_failures == 1


async def test_a_late_success_with_a_replaced_token_keeps_the_backoff(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    await client._async_ensure_token()
    old_token = client._token
    client._token = "replacement"
    client._token_retrieval_failures = 2

    client._token_accepted(old_token)  # type: ignore[arg-type]

    assert client._token_retrieval_failures == 2


async def test_404_is_a_request_error(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    """A 404 isn't silently treated as an empty success (e.g. "no account")."""
    token_mock.post(BACKEND_GRAPHQL_URL, status=404, text="not found")

    with pytest.raises(OctopusChargeRequestError):
        await client.async_stop_boost(ACCOUNT_NUMBER, DEVICE_UUID)


async def test_unexpected_charge_point_data_is_a_request_error(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    token_mock.post(
        BACKEND_GRAPHQL_URL,
        json={"data": {"chargePointsAtLocation": {"chargePoints": [{"model": "x"}]}}},
    )

    with pytest.raises(OctopusChargeRequestError):
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)


async def test_graphql_errors_are_request_errors(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    token_mock.post(
        BACKEND_GRAPHQL_URL,
        json={"errors": [{"message": "Something odd.", "extensions": {}}]},
    )

    with pytest.raises(OctopusChargeRequestError) as err:
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)

    assert not isinstance(err.value, OctopusChargeAuthError)
    assert err.value.errors == ["Something odd"]


async def test_graphql_errors_carry_diagnostic_details(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    token_mock.post(
        BACKEND_GRAPHQL_URL,
        json={
            "data": {"chargePointConfiguration": {"serialNumber": "SECRET-SERIAL"}},
            "errors": [
                {
                    "message": "Internal server error.",
                    "path": ["chargePointConfiguration"],
                    "extensions": {"errorClass": "INTERNAL", "nested": {"a": 1}},
                }
            ],
        },
        headers={
            "x-request-id": "req-123",
            "cf-ray": "ray-456",
            "set-cookie": "session=SECRET-COOKIE",
        },
    )

    with pytest.raises(OctopusChargeRequestError) as err:
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)

    details = err.value.details
    assert details["request"] == "chargepoint-configuration"
    assert details["status"] == 200
    assert details["partial_data"] is True
    assert details["errors"] == [
        {
            "message": "Internal server error.",
            "path": ["chargePointConfiguration"],
            "extensions": {"errorClass": "INTERNAL"},
        }
    ]
    assert details["headers"]["x-request-id"] == "req-123"
    assert details["headers"]["cf-ray"] == "ray-456"

    # Only the listed headers, and nothing from the response data.
    logged = err.value.details_text()
    assert "SECRET-COOKIE" not in logged
    assert "SECRET-SERIAL" not in logged
    assert "set-cookie" not in logged


async def test_http_errors_carry_diagnostic_details(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    token_mock.post(
        BACKEND_GRAPHQL_URL,
        status=502,
        text="<html>Bad gateway</html>",
        headers={"server": "cloudfront", "via": "1.1 abc.cloudfront.net"},
    )

    with pytest.raises(OctopusChargeServerError) as err:
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)

    assert err.value.details["status"] == 502
    assert err.value.details["body"] == "<html>Bad gateway</html>"
    assert err.value.details["headers"]["server"] == "cloudfront"
    assert "seconds" in err.value.details


async def test_connection_errors_carry_details(
    client: OctopusChargeApiClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(KRAKEN_GRAPHQL_URL, exc=TimeoutError())

    with pytest.raises(OctopusChargeConnectionError) as err:
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)

    assert err.value.details["error"] == "TimeoutError"
    assert err.value.details["request"] == "refresh-token"
    assert "seconds" in err.value.details


async def test_graphql_auth_error_codes_are_auth_errors(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    token_mock.post(
        BACKEND_GRAPHQL_URL,
        json={
            "errors": [{"message": "Bad", "extensions": {"errorCode": "KT-CT-1111"}}]
        },
    )

    with pytest.raises(OctopusChargeAuthError):
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)


@pytest.mark.parametrize(
    "errors",
    [
        ["Internal server error."],
        "Internal server error.",
        [{"message": "Internal server error.", "extensions": "INTERNAL"}],
    ],
)
async def test_malformed_graphql_errors_are_request_errors(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker, errors: Any
) -> None:
    """Errors that aren't objects (or lists of them) mustn't crash the parsing."""
    token_mock.post(BACKEND_GRAPHQL_URL, json={"errors": errors})

    with pytest.raises(OctopusChargeRequestError) as err:
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)

    assert not isinstance(err.value, OctopusChargeAuthError)
    assert err.value.errors == ["Internal server error"]
    assert err.value.details["errors"][0]["message"] == "Internal server error."


async def test_connection_errors(
    client: OctopusChargeApiClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(KRAKEN_GRAPHQL_URL, exc=aiohttp.ClientConnectionError())

    with pytest.raises(OctopusChargeConnectionError):
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)


async def test_timeouts_are_connection_errors(
    client: OctopusChargeApiClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(KRAKEN_GRAPHQL_URL, exc=TimeoutError())

    with pytest.raises(OctopusChargeConnectionError):
        await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)


# --- queries and mutations --------------------------------------------------


def respond_in_sequence(mocker: AiohttpClientMocker, url: str, *bodies: Any) -> None:
    """Answer successive POSTs to ``url`` with each body in turn."""
    responses = iter(bodies)

    async def _respond(
        method: str, request_url: Any, data: Any
    ) -> AiohttpClientMockResponse:
        return AiohttpClientMockResponse(method, request_url, json=next(responses))

    mocker.post(url, side_effect=_respond)


async def test_get_property_ids(
    client: OctopusChargeApiClient, aioclient_mock: AiohttpClientMocker
) -> None:
    # The token and account queries share a URL, so answer them in order.
    respond_in_sequence(
        aioclient_mock,
        KRAKEN_GRAPHQL_URL,
        token_response(),
        {
            "data": {
                "account": {"number": ACCOUNT_NUMBER},
                "properties": [{"id": PROPERTY_ID, "occupancyPeriods": []}],
            }
        },
    )

    assert await client.async_get_property_ids(ACCOUNT_NUMBER) == [PROPERTY_ID]


async def test_get_property_ids_for_unknown_account(
    client: OctopusChargeApiClient, aioclient_mock: AiohttpClientMocker
) -> None:
    respond_in_sequence(
        aioclient_mock,
        KRAKEN_GRAPHQL_URL,
        token_response(),
        {"data": {"account": None, "properties": []}},
    )

    assert await client.async_get_property_ids(ACCOUNT_NUMBER) is None


async def test_get_charge_points(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    token_mock.post(
        BACKEND_GRAPHQL_URL,
        json={
            "data": {"chargePointsAtLocation": {"chargePoints": [charge_point_data()]}}
        },
    )

    charge_points = await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)

    assert [charge_point.deviceUUID for charge_point in charge_points] == [DEVICE_UUID]


async def test_get_schedules_when_unavailable(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    token_mock.post(BACKEND_GRAPHQL_URL, json={"data": {"chargePointSchedules": None}})

    assert await client.async_get_schedules(ACCOUNT_NUMBER, DEVICE_UUID) is None


async def test_get_schedules_tolerates_days_without_periods(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    token_mock.post(
        BACKEND_GRAPHQL_URL,
        json={
            "data": {
                "chargePointSchedules": {
                    "schedules": [
                        {"day": "MONDAY", "chargePointScheduleSettings": None}
                    ]
                }
            }
        },
    )

    schedules = await client.async_get_schedules(ACCOUNT_NUMBER, DEVICE_UUID)

    assert schedules is not None
    assert schedules[0].chargePointScheduleSettings == []


async def test_mutations_send_the_expected_values(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    token_mock.post(BACKEND_GRAPHQL_URL, json={"data": {}})

    await client.async_set_eco_mode(ACCOUNT_NUMBER, DEVICE_UUID, True)
    await client.async_set_led_brightness(ACCOUNT_NUMBER, DEVICE_UUID, 40)
    await client.async_set_control_mode(ACCOUNT_NUMBER, DEVICE_UUID, "MANUAL")

    queries = [call[2]["query"] for call in calls_to(token_mock, BACKEND_GRAPHQL_URL)]
    assert "true" in queries[0] and DEVICE_UUID in queries[0]
    assert "40" in queries[1]
    assert "MANUAL" in queries[2]


async def test_invalid_control_mode_is_rejected(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    with pytest.raises(ValueError):
        await client.async_set_control_mode(ACCOUNT_NUMBER, DEVICE_UUID, "X } evil")

    assert calls_to(token_mock, BACKEND_GRAPHQL_URL) == []


async def test_string_values_cannot_break_out_of_the_query(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    token_mock.post(BACKEND_GRAPHQL_URL, json={"data": {}})

    await client.async_set_eco_mode(ACCOUNT_NUMBER, 'x"} evil: "', True)

    query = calls_to(token_mock, BACKEND_GRAPHQL_URL)[0][2]["query"]
    assert r'"x\"} evil: \""' in query


async def test_boost_mutations(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    token_mock.post(BACKEND_GRAPHQL_URL, json={"data": {}})

    await client.async_start_boost(
        ACCOUNT_NUMBER, DEVICE_UUID, dt_util.utcnow() + timedelta(hours=1)
    )
    await client.async_stop_boost(ACCOUNT_NUMBER, DEVICE_UUID)

    assert len(calls_to(token_mock, BACKEND_GRAPHQL_URL)) == 2


# --- live power stream ------------------------------------------------------


async def test_stream_yields_readings(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    body = (
        "--graphql\nContent-Type: application/json\n\n"
        '{"payload":{"data":{"electricChargerPowerReadings":'
        '{"value":7.2,"unit":"KILOWATT"}}}}\n--graphql\n'
    )
    token_mock.post(BACKEND_GRAPHQL_URL, text=body)

    readings = [
        reading
        async for reading in client.async_stream_power(ACCOUNT_NUMBER, DEVICE_UUID)
    ]

    assert [reading.value for reading in readings if reading] == [7.2]
    headers = calls_to(token_mock, BACKEND_GRAPHQL_URL)[0][3]
    assert "subscriptionSpec=1.0" in headers["Accept"]


async def test_stream_refusal_leaves_the_shared_token_alone(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    """Status polls still use the token; reconnects mustn't churn new ones."""
    token_mock.post(BACKEND_GRAPHQL_URL, side_effect=backend_sequence(200, 401))
    await client.async_get_charge_points(ACCOUNT_NUMBER, PROPERTY_ID)

    for _ in range(3):
        with pytest.raises(OctopusChargeRequestError) as exc_info:
            async for _ in client.async_stream_power(ACCOUNT_NUMBER, DEVICE_UUID):
                pass
        assert not isinstance(exc_info.value, OctopusChargeAuthError)

    assert len(calls_to(token_mock, KRAKEN_GRAPHQL_URL)) == 1


async def test_stream_of_only_errors_does_not_confirm_the_token(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    """A 200 carrying nothing but error parts says nothing good about the token."""
    await client._async_ensure_token()
    client._token_retrieval_failures = 2
    token_mock.post(
        BACKEND_GRAPHQL_URL,
        text=(
            "--graphql\r\ncontent-type: application/json\r\n\r\n"
            '{"payload":null,"errors":[{"message":"JWT has expired"}]}\r\n'
            "--graphql--"
        ),
    )

    readings = [r async for r in client.async_stream_power(ACCOUNT_NUMBER, DEVICE_UUID)]

    assert readings == []
    assert client._token_retrieval_failures == 2


async def test_stream_cloudfront_403_keeps_the_token(
    client: OctopusChargeApiClient, token_mock: AiohttpClientMocker
) -> None:
    """A CloudFront 403 is a server error, as for every other request."""
    token_mock.post(
        BACKEND_GRAPHQL_URL,
        status=403,
        text="ERROR: The request could not be satisfied",
    )

    for _ in range(3):
        with pytest.raises(OctopusChargeServerError):
            async for _ in client.async_stream_power(ACCOUNT_NUMBER, DEVICE_UUID):
                pass

    assert len(calls_to(token_mock, KRAKEN_GRAPHQL_URL)) == 1


async def test_stream_connection_errors(
    client: OctopusChargeApiClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(KRAKEN_GRAPHQL_URL, json=token_response())
    aioclient_mock.post(BACKEND_GRAPHQL_URL, exc=aiohttp.ClientConnectionError())

    with pytest.raises(OctopusChargeConnectionError):
        async for _ in client.async_stream_power(ACCOUNT_NUMBER, DEVICE_UUID):
            pass


async def test_stream_errors_carry_details(
    client: OctopusChargeApiClient, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(KRAKEN_GRAPHQL_URL, json=token_response())
    aioclient_mock.post(
        BACKEND_GRAPHQL_URL, status=401, headers={"x-request-id": "req-123"}
    )

    with pytest.raises(OctopusChargeRequestError) as err:
        async for _ in client.async_stream_power(ACCOUNT_NUMBER, DEVICE_UUID):
            pass

    assert err.value.details["request"] == "chargepoint-power-stream"
    assert err.value.details["status"] == 401
    assert err.value.details["headers"]["x-request-id"] == "req-123"

    aioclient_mock.clear_requests()
    aioclient_mock.post(KRAKEN_GRAPHQL_URL, json=token_response())
    aioclient_mock.post(BACKEND_GRAPHQL_URL, exc=aiohttp.ClientConnectionError())

    with pytest.raises(OctopusChargeConnectionError) as err:
        async for _ in client.async_stream_power(ACCOUNT_NUMBER, DEVICE_UUID):
            pass

    assert err.value.details["error"] == "ClientConnectionError"
    assert "seconds" in err.value.details
