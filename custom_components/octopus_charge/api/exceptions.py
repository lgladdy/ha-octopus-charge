"""Exceptions raised by the Octopus Charge API client."""

from __future__ import annotations


class OctopusChargeError(Exception):
    """Base class for all API errors."""


class OctopusChargeServerError(OctopusChargeError):
    """Octopus returned a server-side (5xx / rate limit) error. Usually transient."""


class OctopusChargeConnectionError(OctopusChargeError):
    """Could not reach Octopus (timeout or network failure)."""


class OctopusChargeRequestError(OctopusChargeError):
    """Octopus rejected the request."""

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        """Initialise the error."""
        super().__init__(message)
        self.errors = errors or []


class OctopusChargeAuthError(OctopusChargeRequestError):
    """The API key was rejected."""
