"""Open-Meteo daily forecast client and clear-sky sunny_score for BC campsites.

Fetches a 14-day daily forecast per campground coordinate from the Open-Meteo
free API (no key required, covers all of Canada). Computes a 0-100 sunny_score
for each park-day, weighted toward clear skies and penalised for precipitation
and temperature extremes.

This module has NO database writes. It is called by campsites.jobs (the hourly
refresh job). It is excluded from the public binary.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import random
from dataclasses import dataclass

import httpx

logger = logging.getLogger("monolith.campsites")

# ---------------------------------------------------------------------------
# Tuning constants (product knobs)
# ---------------------------------------------------------------------------

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
FORECAST_DAYS = 14

# Weight applied to the clear-sky component (100 - cloud_cover%).
CLEAR_WEIGHT = 1.0

# Precipitation penalty: per-percent-probability and per-mm terms.
PRECIP_PENALTY_PER_PCT = 0.4
PRECIP_PENALTY_PER_MM = 8.0
PRECIP_PENALTY_CAP = 45.0

# Temperature comfort band (Celsius). Outside this range a penalty applies.
COMFORT_LO: float = 15.0
COMFORT_HI: float = 28.0
TEMP_PENALTY_PER_DEG = 2.0
TEMP_PENALTY_CAP = 20.0

# Thresholds for classifying a day as "good".
GOOD_SCORE_MIN = 60
GOOD_PRECIP_MAX_MM = 3.0


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class WxDay:
    """Weather forecast for one campground on one calendar date."""

    date: datetime.date
    cloud_cover: float | None
    precip_sum: float | None
    precip_prob: float | None
    temp_max: float | None
    wind_max: float | None
    sunny_score: int
    is_good: bool


# ---------------------------------------------------------------------------
# Pure scoring functions (unit-tested)
# ---------------------------------------------------------------------------


def sunny_score(
    cloud_cover: float | None,
    precip_sum: float | None,
    precip_prob: float | None,
    temp_max: float | None,
) -> int:
    """Compute a 0-100 clear-sky score for one park-day.

    Higher is better (clearer, drier, more comfortable). Formula:
      base = (100 - cloud_cover%) * CLEAR_WEIGHT
      precip_pen = min(CAP, prob * PRECIP_PENALTY_PER_PCT + sum * PRECIP_PENALTY_PER_MM)
      temp_pen = 0 inside [COMFORT_LO, COMFORT_HI]; scaled and capped outside
      score = clamp(0, 100, round(base - precip_pen - temp_pen))

    Any None input is treated as 0 for the purpose of the formula; temp_max
    None means no temperature penalty.
    """
    base = (100.0 - (cloud_cover or 0.0)) * CLEAR_WEIGHT

    precip_pen = min(
        PRECIP_PENALTY_CAP,
        (precip_prob or 0.0) * PRECIP_PENALTY_PER_PCT
        + (precip_sum or 0.0) * PRECIP_PENALTY_PER_MM,
    )

    if temp_max is None:
        temp_pen = 0.0
    elif temp_max < COMFORT_LO:
        temp_pen = min(TEMP_PENALTY_CAP, (COMFORT_LO - temp_max) * TEMP_PENALTY_PER_DEG)
    elif temp_max > COMFORT_HI:
        temp_pen = min(TEMP_PENALTY_CAP, (temp_max - COMFORT_HI) * TEMP_PENALTY_PER_DEG)
    else:
        temp_pen = 0.0

    return max(0, min(100, round(base - precip_pen - temp_pen)))


def is_good_day(score: int, precip_sum: float | None) -> bool:
    """True when a day clears the minimum score and precipitation thresholds."""
    return score >= GOOD_SCORE_MIN and (precip_sum or 0.0) < GOOD_PRECIP_MAX_MM


def parse_forecast(daily_json: dict, ndays: int = FORECAST_DAYS) -> list[WxDay]:
    """Parse an Open-Meteo ``daily`` response dict into WxDay records.

    The five data arrays (cloud_cover_mean, precipitation_sum,
    precipitation_probability_max, temperature_2m_max, wind_speed_10m_max)
    are parallel to the "time" array. Missing keys and short arrays are
    handled by a safe index helper that returns None for out-of-range
    positions. At most ``ndays`` rows are returned.
    """

    def _get(arr: list | None, i: int) -> float | None:
        if arr is None or i >= len(arr):
            return None
        return arr[i]

    times: list[str] = daily_json.get("time") or []
    clouds = daily_json.get("cloud_cover_mean")
    precip_sums = daily_json.get("precipitation_sum")
    precip_probs = daily_json.get("precipitation_probability_max")
    temps = daily_json.get("temperature_2m_max")
    winds = daily_json.get("wind_speed_10m_max")

    rows: list[WxDay] = []
    for i, date_str in enumerate(times[:ndays]):
        date = datetime.date.fromisoformat(date_str)
        cloud = _get(clouds, i)
        psum = _get(precip_sums, i)
        pprob = _get(precip_probs, i)
        tmax = _get(temps, i)
        wmax = _get(winds, i)

        score = sunny_score(cloud, psum, pprob, tmax)
        good = is_good_day(score, psum)

        rows.append(
            WxDay(
                date=date,
                cloud_cover=cloud,
                precip_sum=psum,
                precip_prob=pprob,
                temp_max=tmax,
                wind_max=wmax,
                sunny_score=score,
                is_good=good,
            )
        )

    return rows


# ---------------------------------------------------------------------------
# Async network functions (caller passes an httpx.AsyncClient)
# ---------------------------------------------------------------------------


async def fetch_forecast(
    client: httpx.AsyncClient, lat: float, lon: float, tz: str
) -> dict | None:
    """GET a 14-day daily forecast from Open-Meteo for one coordinate pair.

    Returns the full parsed JSON response or None on any error (logged at
    WARNING). The caller treats a None result as "skip this park for this
    run" (stale data beats a missing entry).
    """
    params = {
        "latitude": round(lat, 4),
        "longitude": round(lon, 4),
        "daily": ",".join(
            [
                "cloud_cover_mean",
                "precipitation_sum",
                "precipitation_probability_max",
                "temperature_2m_max",
                "wind_speed_10m_max",
            ]
        ),
        "forecast_days": FORECAST_DAYS,
        "timezone": tz,
    }
    try:
        response = await client.get(OPEN_METEO_URL, params=params)
        response.raise_for_status()
        return response.json()
    except Exception:
        logger.warning(
            "campsites weather: fetch failed for (%s, %s)", lat, lon, exc_info=True
        )
        return None


async def fetch_all_weather(
    client: httpx.AsyncClient,
    coords: list[tuple[int, float, float, str]],
) -> dict[int, list[WxDay]]:
    """Fetch and parse forecasts for all campgrounds.

    ``coords`` is a list of (resource_location_id, lat, lon, iana_tz). Parks
    whose fetch returns None are skipped (logged inside fetch_forecast). A
    small deterministic per-index sleep paces the requests without hammering
    the free API.

    Caller is expected to pass httpx.AsyncClient(timeout=25).
    """
    results: dict[int, list[WxDay]] = {}

    for i, (rid, lat, lon, tz) in enumerate(coords):
        await asyncio.sleep(random.Random(i).uniform(0.1, 0.2))
        data = await fetch_forecast(client, lat, lon, tz)
        if data is None:
            continue
        try:
            rows = parse_forecast(data.get("daily", {}))
        except Exception:
            logger.warning(
                "campsites weather: parse failed for park %d", rid, exc_info=True
            )
            continue
        results[rid] = rows

    return results
