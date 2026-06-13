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

from stars.scoring import WeatherData, darkness_factor, quality_score

logger = logging.getLogger("monolith.stars.forecast")

MET_NORWAY_URL = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
USER_AGENT = os.environ.get(
    "STARS_USER_AGENT", "jomcgi-homelab-stars/1.0 https://jomcgi.dev"
)
RATE_LIMIT_PER_SEC = int(os.environ.get("STARS_RATE_LIMIT", "15"))
HTTP_TIMEOUT = 30.0


def score_location(loc: dict, forecast: dict) -> list[dict]:
    """All dark hours for one site ranked by quality, sorted by time ascending.

    ADR 007: every civil-dark hour (sun below -6 deg) is kept and scored by the
    continuous quality Q = D x C x W, so summer nights surface the best
    available windows instead of going empty. Hours that are not dark, or are
    dark but hopeless (Q == 0, e.g. fully clouded), are dropped.

    Each returned dict matches the stars.site_hours columns (minus site_id /
    fetched_at, which the job sets): time, score, sun_elevation_deg,
    cloud_area_fraction, relative_humidity, wind_speed, air_temperature,
    dew_spread, symbol.
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
        if darkness_factor(e) <= 0.0:
            continue  # daylight / brighter than civil twilight
        instant = entry.get("data", {}).get("instant", {}).get("details", {})
        next_1h = entry.get("data", {}).get("next_1_hours", {})
        try:
            weather = WeatherData(
                cloud_area_fraction=instant.get("cloud_area_fraction", 100),
                relative_humidity=instant.get("relative_humidity", 100),
                fog_area_fraction=instant.get("fog_area_fraction", 0),
                wind_speed=instant.get("wind_speed", 0),
                air_temperature=instant.get("air_temperature", 10),
                dew_point_temperature=instant.get("dew_point_temperature", 5),
                air_pressure_at_sea_level=instant.get(
                    "air_pressure_at_sea_level", 1013.25
                ),
            )
        except Exception as exc:
            logger.debug("skip malformed weather entry at %s: %s", time_str, exc)
            continue
        q = quality_score(weather, e)
        if q <= 0.0:
            continue  # dark but hopeless (e.g. fully clouded)
        hours.append(
            {
                "time": time_str,
                "score": round(q, 1),
                "sun_elevation_deg": round(e, 1),
                "cloud_area_fraction": weather.cloud_area_fraction,
                "relative_humidity": weather.relative_humidity,
                "wind_speed": weather.wind_speed,
                "air_temperature": weather.air_temperature,
                "dew_spread": round(
                    weather.air_temperature - weather.dew_point_temperature, 1
                ),
                "symbol": next_1h.get("summary", {}).get("symbol_code", ""),
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
