"""Constants for the Octopus Charge integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "octopus_charge"

CONF_ACCOUNT_NUMBER = "account_number"

MANUFACTURER = "Octopus Energy"
DEVICE_NAME = "Octopus Charge"

# How often the charge point status and configuration is refreshed.
SCAN_INTERVAL = timedelta(minutes=1)

# The weekly schedule rarely changes (only when edited in the Octopus app), so
# it is polled far less often than the charger's status.
SCHEDULE_SCAN_INTERVAL = timedelta(minutes=10)

# After boost is toggled (or a scheduled period starts/ends) the status is
# polled much more frequently for a short while so dependent entities pick up
# the change quickly, rather than waiting up to a full scan interval.
BURST_INTERVAL_SECONDS = 10
BURST_DURATION_SECONDS = 60

# Start the post-transition burst slightly ahead of the scheduled boundary.
SCHEDULE_BURST_LEAD_SECONDS = 5

# Common schedule boundaries (popular off-peak start times) are shared across
# many accounts, so spread the burst trigger out to avoid every account
# hitting Octopus on the same second.
SCHEDULE_BURST_JITTER_MAX_SECONDS = 20

# Never wait longer than this between attempts after repeated failures.
MAX_RETRY_SECONDS = 30 * 60

# Octopus's API regularly fails for a few minutes at a time. For this long after
# the first failed status poll, entities keep the last known data (and polling
# carries on at the normal rate) instead of going unavailable.
OUTAGE_TOLERANCE = timedelta(minutes=15)

DEFAULT_BOOST_DURATION = timedelta(hours=1)

# How long a value just sent to the charger is shown while polls still return
# the old one (Octopus can take a while to reflect a change).
OPTIMISTIC_TIMEOUT = timedelta(minutes=2)

OPERATIONAL_STATES = [
    "CHARGING",
    "BOOST_CHARGING",
    "NOT_CHARGING",
    "UNPLUGGED",
    "ONBOARDING",
    "SMART_CONTROL_SCHEDULED",
]
CHARGING_STATES = ("CHARGING", "BOOST_CHARGING")
BOOSTING_STATES = ("BOOST_CHARGING",)

CHARGING_METHODS = ["SCHEDULED", "ON_DEMAND"]

# Live power readings arrive over a long-lived stream; reconnect with backoff
# while the charger still reports that it is charging.
STREAM_INITIAL_RECONNECT_DELAY_SECONDS = 5
STREAM_MAX_RECONNECT_DELAY_SECONDS = 60
