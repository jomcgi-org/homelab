"""Clear-dark-hour predicates for the stars dark-sky planner (v2).

The stars v2 metric replaces the continuous quality score Q = D x C x W with a
concrete, honest count of "clear dark hours": hours where the sun is far enough
below the horizon to be properly dark AND the sky is clear enough to actually
see stars. Pure functions, no I/O, no external deps.

A clear-dark hour is the unit of value. A dark hour (sun below the nautical
threshold) is the denominator for the clarity rate (clear_dark_hours over
dark_hours). The same thresholds are replicated by the offline ERA5 backfill
(grid_gen/backfill_climatology.py); keep them in sync.
"""

NAUTICAL_DARK_DEG = -12.0  # sun below this = "dark" for the clear-dark metric
CLEAR_CLOUD_MAX_PCT = 10.0  # cloud cover (%) strictly below this = "clear"


def is_dark_hour(sun_elevation_deg: float) -> bool:
    """True when the sun is far enough below the horizon to count as a dark hour
    (nautical/astronomical), the denominator for the clear-dark rate."""
    return sun_elevation_deg < NAUTICAL_DARK_DEG


def is_clear_dark_hour(sun_elevation_deg: float, cloud_area_fraction: float) -> bool:
    """The stars v2 unit of value: a dark hour (sun < -12 deg) that is also clear
    (cloud < 10%). Counted per site per month-of-year (live + ERA5 climatology)."""
    return is_dark_hour(sun_elevation_deg) and cloud_area_fraction < CLEAR_CLOUD_MAX_PCT
