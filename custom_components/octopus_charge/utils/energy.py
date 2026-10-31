"""Energy integration helpers."""

from __future__ import annotations

# A single integration step shouldn't span longer than this - guards against
# fabricating a spike of "energy" from a long gap between readings (e.g. HA
# restarting mid-charge), since live readings normally arrive far more often
# than this while actually charging.
MAX_INTEGRATION_STEP_HOURS = 0.1  # ~6 minutes


def integrate_energy_kwh(
    current_total_kwh: float,
    power_kw: float,
    elapsed_hours: float,
    max_step_hours: float = MAX_INTEGRATION_STEP_HOURS,
) -> float:
    """Add one Riemann-sum step to a running energy total.

    A pure function so the integration/guard logic can be unit tested without a
    real hass instance or entity plumbing. Returns the total unchanged if
    elapsed_hours is non-positive (clock went backwards/same instant) or exceeds
    max_step_hours (too long a gap to trust - see above).
    """
    if elapsed_hours <= 0 or elapsed_hours >= max_step_hours:
        return current_total_kwh

    return current_total_kwh + (power_kw * elapsed_hours)
