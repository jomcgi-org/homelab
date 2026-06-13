"""MET Norway fetch + astronomy scoring for the stars seed sites."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

import httpx
from astral import LocationInfo
from astral.sun import elevation

from stars.scoring import WeatherData, calculate_astronomy_score
from stars.seed import SCOTLAND_DARK_SKY_LOCATIONS, SeedLocation

logger = logging.getLogger("monolith.stars.forecast")

MET_NORWAY_URL = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
USER_AGENT = os.environ.get(
    "STARS_USER_AGENT", "jomcgi-homelab-stars/1.0 https://jomcgi.dev"
)
RATE_LIMIT_PER_SEC = int(os.environ.get("STARS_RATE_LIMIT", "15"))
MIN_DISPLAY_SCORE = int(os.environ.get("STARS_MIN_DISPLAY_SCORE", "60"))
NAUTICAL_TWILIGHT_DEG = -12.0
HTTP_TIMEOUT = 30.0


def score_location(loc: SeedLocation, forecast: dict) -> list[dict]:
    """All qualifying dark hours for one site, sorted by time ascending.

    Each returned dict matches the stars.site_hours columns (minus site_id /
    fetched_at, which the job sets): time, score, cloud_area_fraction,
    relative_humidity, wind_speed, air_temperature, dew_spread, symbol.
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
            if elevation(observer, t) > NAUTICAL_TWILIGHT_DEG:
                continue
        except Exception as exc:  # pragma: no cover - astral edge cases
            logger.debug("astral elevation failed for %s at %s: %s", loc["id"], t, exc)
            continue
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
        score = calculate_astronomy_score(weather)
        if score < MIN_DISPLAY_SCORE:
            continue
        hours.append(
            {
                "time": time_str,
                "score": round(score, 1),
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
    client: httpx.AsyncClient, loc: SeedLocation
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


async def fetch_all() -> dict[str, list[dict]]:
    """Map site id -> scored future hours, for sites that fetched successfully."""
    semaphore = asyncio.Semaphore(RATE_LIMIT_PER_SEC)
    async with httpx.AsyncClient(timeout=httpx.Timeout(HTTP_TIMEOUT)) as client:

        async def _bounded(loc: SeedLocation):
            async with semaphore:
                result = await _fetch_and_score(client, loc)
                await asyncio.sleep(1.0 / RATE_LIMIT_PER_SEC)  # stay under MET 20/s
                return result

        results = await asyncio.gather(
            *(_bounded(loc) for loc in SCOTLAND_DARK_SKY_LOCATIONS)
        )
    return {sid: hours for sid, hours in results if hours is not None}
