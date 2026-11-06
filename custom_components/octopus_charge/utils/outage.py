"""Riding out short runs of failed polls."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..api.exceptions import OctopusChargeError
from ..const import OUTAGE_TOLERANCE


class OutageTracker:
    """Tracks a run of failed polls so a coordinator can ride out short ones.

    Octopus's API fails for a few minutes at a time, so for ``OUTAGE_TOLERANCE``
    after the first failed poll a coordinator keeps serving its last data (and
    polls at the normal rate, to notice the recovery quickly) rather than making
    its entities unavailable.
    """

    def __init__(self, logger: logging.Logger, what: str) -> None:
        """Initialise the tracker; ``what`` names the data in log messages."""
        self._logger = logger
        self._what = what
        self.failing_since: datetime | None = None
        self.failed_polls = 0
        # The most recent failure (kept after recovery), for diagnostics.
        self.last_failure: dict[str, Any] | None = None

    @property
    def failing(self) -> bool:
        """Return whether the latest poll failed."""
        return self.failing_since is not None

    def failure(
        self, started: datetime, err: OctopusChargeError, *, have_data: bool
    ) -> bool:
        """Record a failed poll.

        Returns whether to keep serving the last data: only while there is some,
        and the outage is still within the tolerance.
        """
        if self.failing_since is None:
            self.failing_since = started
        self.failed_polls += 1
        self.last_failure = {
            "time": started.isoformat(),
            "error": str(err),
            "details": err.details,
        }

        if not have_data or started - self.failing_since >= OUTAGE_TOLERANCE:
            return False

        # The first failure of an outage is worth a warning; the rest are noise.
        self._logger.log(
            logging.WARNING if started == self.failing_since else logging.DEBUG,
            "Error retrieving %s, using the last known values for up to %s: %s",
            self._what,
            OUTAGE_TOLERANCE,
            err.describe(),
        )
        return True

    def success(self, started: datetime) -> None:
        """Record a successful poll, ending any outage."""
        if self.failing_since is not None:
            self._logger.info(
                "%s retrieved again after %s (%d failed polls)",
                self._what.capitalize(),
                started - self.failing_since,
                self.failed_polls,
            )
        self.failing_since = None
        self.failed_polls = 0
