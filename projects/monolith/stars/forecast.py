"""MET Norway fetch + astronomy scoring for the stars sites.

Sites come from the stars.sites table (sourced from the light-pollution grid,
ADR 006). Callers pass a list of site dicts (id/lat/lon/altitude_m); this module
fetches and scores each one's dark hours.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

import httpx
from astral import LocationInfo
from astral.sun import elevation

from stars.scoring import CLEAR_CLOUD_MAX_PCT, is_dark_hour, is_twilight_hour

logger = logging.getLogger("monolith.stars.forecast")

MET_NORWAY_URL = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
USER_AGENT = os.environ.get(
    "STARS_USER_AGENT", "jomcgi-homelab-stars/1.0 https://jomcgi.dev"
)
RATE_LIMIT_PER_SEC = int(os.environ.get("STARS_RATE_LIMIT", "15"))
HTTP_TIMEOUT = 30.0


def score_location(loc: dict, forecast: dict) -> list[dict]:
    """All twilight-or-darker hours for one site, sorted by time ascending (v2).

    Every hour with the sun below the -10 deg twilight floor is kept and tagged
    with ``is_clear`` (cloud < 10%) and ``dark`` (sun < -12 deg, true nautical
    dark). The gate is the twilight floor, not the dark threshold, so the LIVE
    layer still has windows to surface during the ~7-week midsummer stretch when
    Scotland gets no astronomical darkness at all; ``dark`` distinguishes the
    true-dark hours from the twilight-only fallback. Hours brighter than -10
    (daylight or shallower twilight) are dropped. A kept-but-cloudy hour is not
    dropped: it is kept with ``is_clear`` False so the read path can still count
    it toward the dark/twilight denominators.

    Each returned dict carries the fields the job and the read path need (minus
    site_id / fetched_at, which the job sets): time, sun_elevation_deg,
    cloud_area_fraction, air_temperature, dew_spread, symbol, is_clear, dark.
    """
    observer = LocationInfo(latitude=loc["lat"], longitude=loc["lon"]).observer
    hours: list[dict] = []
    for entry in forecast.get("properties", {}).get("timeseries", []):
        time_str = entry.get("time", "")
        try:
            t = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        try:
            e = elevation(observer, t)
        except Exception as exc:  # pragma: no cover - astral edge cases
            logger.debug("astral elevation failed for %s at %s: %s", loc["id"], t, exc)
            continue
        if not is_twilight_hour(e):
            continue  # daylight / brighter than the -10 deg twilight floor
        instant = entry.get("data", {}).get("instant", {}).get("details", {})
        next_1h = entry.get("data", {}).get("next_1_hours", {})
        # Missing cloud defaults to overcast (100) so an absent reading counts as
        # not-clear rather than silently qualifying as a clear-dark hour.
        cloud = instant.get("cloud_area_fraction", 100)
        air_temperature = instant.get("air_temperature", 10)
        dew_point = instant.get("dew_point_temperature", 5)
        hours.append(
            {
                "time": time_str,
                "sun_elevation_deg": round(e, 1),
                "cloud_area_fraction": cloud,
                "air_temperature": air_temperature,
                "dew_spread": round(air_temperature - dew_point, 1),
                "symbol": next_1h.get("summary", {}).get("symbol_code", ""),
                "is_clear": cloud < CLEAR_CLOUD_MAX_PCT,
                # True nautical dark (sun < -12), vs a twilight-only fallback
                # hour (-12 <= sun < -10). The read path recomputes this from
                # sun_elevation_deg, but emitting it keeps the dict honest.
                "dark": is_dark_hour(e),
            }
        )
    hours.sort(key=lambda h: h["time"])
    return hours


async def _fetch_and_score(
    client: httpx.AsyncClient, loc: dict
) -> tuple[str, list[dict] | None]:
    """Fetch + score one site. None means the fetch failed (keep stale rows)."""
    try:
        resp = await client.get(
            MET_NORWAY_URL,
            params={
                "lat": loc["lat"],
                "lon": loc["lon"],
                "altitude": loc["altitude_m"],
            },
            headers={"User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("stars forecast fetch failed for %s: %s", loc["id"], exc)
        return loc["id"], None
    return loc["id"], score_location(loc, resp.json())


async def fetch_all(sites: list[dict]) -> dict[str, list[dict]]:
    """Map site id -> scored future hours, for sites that fetched successfully.

    ``sites`` is the list of grid-sourced site dicts (id/lat/lon/altitude_m)
    loaded from the stars.sites table by the refresh job.
    """
    semaphore = asyncio.Semaphore(RATE_LIMIT_PER_SEC)
    async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT)) as client:

        async def _bounded(loc: dict):
            async with semaphore:
                result = await _fetch_and_score(client, loc)
                await asyncio.sleep(1.0 / RATE_LIMIT_PER_SEC)  # stay under MET 20/s
                return result

        results = await asyncio.gather(*(_bounded(loc) for loc in sites))
    return {sid: hours for sid, hours in results if hours is not None}
