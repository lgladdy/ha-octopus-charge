"""Exceptions raised by the Octopus Charge API client."""

from __future__ import annotations

import json
from typing import Any


class OctopusChargeError(Exception):
    """Base class for all API errors."""

    def __init__(self, *args: object, details: dict[str, Any] | None = None) -> None:
        """Initialise the error.

        ``details`` is diagnostic context for the logs (HTTP status, timing,
        request ids...). It never holds credentials or response data.
        """
        super().__init__(*args)
        self.details = details or {}

    def details_text(self) -> str:
        """Return the details as one compact line, or an empty string."""
        if not self.details:
            return ""
        return json.dumps(self.details, default=str, separators=(",", ":"))

    def describe(self) -> str:
        """Return the message followed by the details, for the logs."""
        details = self.details_text()
        return f"{self} [{details}]" if details else str(self)


class OctopusChargeServerError(OctopusChargeError):
    """Octopus returned a server-side (5xx / rate limit) error. Usually transient."""


class OctopusChargeConnectionError(OctopusChargeError):
    """Could not reach Octopus (timeout or network failure)."""


class OctopusChargeCannotConnectError(OctopusChargeConnectionError):
    """No connection could be made, so the request was never sent."""


class OctopusChargeRequestError(OctopusChargeError):
    """Octopus rejected the request."""

    def __init__(
        self,
        message: str,
        errors: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialise the error."""
        super().__init__(message, details=details)
        self.errors = errors or []


class OctopusChargeAuthError(OctopusChargeRequestError):
    """The API key was rejected."""


class OctopusChargeUnauthenticatedError(OctopusChargeAuthError):
    """Octopus rejected the request's credentials with an HTTP 401/403.

    On a data request this usually means the token was rejected rather than the
    API key, so the client retries once with a freshly retrieved token.
    """
