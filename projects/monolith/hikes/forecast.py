"""met.no forecast fetch and viable-window computation.

Ported from projects/hikes/update_forecast/update.py with fetch split from
the pure logic: fetch_forecast hits locationforecast/2.0/compact, parse_hourly
flattens the timeseries, and compute_windows applies the exact viability
ladder (past hours, 7-day horizon, 07:00-19:00 daylight gate, precip > 2.0 mm,
wind > 80 km/h) and emits the compact window tuples
[timestamp, temp_c, precip_mm, wind_kmh, cloud_pct] with the original
rounding rules so the stored windows match the old bundle format exactly.
"""

import asyncio
import logging
from datetime import datetime, timedelta

import httpx

logger = logging.getLogger("hikes")

FORECAST_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
# met.no requires an identifying User-Agent.
USER_AGENT = "jomcgi.dev/app/hikes (https://github.com/jomcgi/homelab)"
TIMEOUT_SECS = 30.0
# met.no asks for at most 20 req/s; the old code used 20 workers. Stay below.
CONCURRENCY = 10

MAX_PRECIPITATION_MM = 2.0
MAX_WIND_KMH = 80.0
DAYLIGHT_START_HOUR = 7
DAYLIGHT_END_HOUR = 19
FORECAST_HORIZON_DAYS = 7


async def fetch_forecast(
    client: httpx.AsyncClient, lat: float, lon: float
) -> dict | None:
    """Fetch the compact location forecast for one coordinate pair.

    Returns None on any error (logged); the caller treats a missing forecast
    as "keep the previous windows".
    """
    params = {"lat": round(lat, 4), "lon": round(lon, 4)}
    headers = {"User-Agent": USER_AGENT}
    try:
        response = await client.get(
            FORECAST_URL, params=params, headers=headers, timeout=TIMEOUT_SECS
        )
        response.raise_for_status()
        return response.json()
    except Exception:
        logger.warning(
            "hikes forecast: fetch failed for (%s, %s)", lat, lon, exc_info=True
        )
        return None


def parse_hourly(forecast_json: dict | None) -> list[dict]:
    """Flatten a met.no forecast into hourly dicts (port of parse_weather_data).

    Entries without next_1_hours (the 6-hourly tail of the forecast) are
    skipped, exactly as the original did.
    """
    if not forecast_json or "properties" not in forecast_json:
        return []

    timeseries = forecast_json["properties"]["timeseries"]
    hourly_data: list[dict] = []

    for entry in timeseries:
        time_str = entry["time"]
        data = entry["data"]
        instant = data.get("instant", {}).get("details", {})
        next_1_hours = data.get("next_1_hours", {})

        if not next_1_hours:
            continue

        hourly_data.append(
            {
                "time": time_str,
                "temp_c": instant.get("air_temperature"),
                "wind_speed_ms": instant.get("wind_speed"),
                "precipitation_mm": next_1_hours.get("details", {}).get(
                    "precipitation_amount", 0
                ),
                "cloud_area_fraction": instant.get("cloud_area_fraction"),
            }
        )

    return hourly_data


def _is_weather_viable(weather: dict) -> bool:
    """Viability thresholds: precip > 2.0 mm or wind > 80 km/h is not viable."""
    precip = weather.get("precipitation_mm", 0)
    wind_ms = weather.get("wind_speed_ms", 0)
    wind_kmh = wind_ms * 3.6 if wind_ms is not None else 0

    if precip > MAX_PRECIPITATION_MM:
        return False
    if wind_kmh > MAX_WIND_KMH:
        return False
    return True


def compute_windows(
    hourly: list[dict], now: datetime, lat: float, lon: float
) -> list[list]:
    """Compute viable hiking windows from hourly forecast data. Pure.

    Ports the exact filter ladder from the original process_walk: skip past
    hours, skip beyond now + 7 days, skip outside 07:00-19:00 (the parsed
    datetime's hour, as the original is_daylight_hour did; lat/lon are kept
    for signature parity with a future real solar calculation), skip
    non-viable weather. Emits [timestamp, temp_c, precip_mm, wind_kmh,
    cloud_pct] with the original rounding and None-default rules.
    """
    del lat, lon  # Reserved for a real daylight calculation; unused for now.
    horizon = now + timedelta(days=FORECAST_HORIZON_DAYS)
    windows: list[list] = []

    for weather in hourly:
        dt = datetime.fromisoformat(weather["time"])

        if dt < now:
            continue
        if dt > horizon:
            continue
        if not (DAYLIGHT_START_HOUR <= dt.hour <= DAYLIGHT_END_HOUR):
            continue
        if not _is_weather_viable(weather):
            continue

        timestamp = int(dt.timestamp())
        temp_c = round(weather["temp_c"], 1) if weather["temp_c"] is not None else 0
        precip_mm = (
            round(weather["precipitation_mm"], 1)
            if weather["precipitation_mm"] > 0
            else 0
        )
        wind_ms = weather["wind_speed_ms"] or 0
        wind_kmh = round(wind_ms * 3.6)
        cloud_pct = (
            round(weather["cloud_area_fraction"])
            if weather["cloud_area_fraction"] is not None
            else 50
        )

        windows.append([timestamp, temp_c, precip_mm, wind_kmh, cloud_pct])

    return windows


async def fetch_all_windows(
    client: httpx.AsyncClient,
    walks: list[tuple[str, float, float]],
    now: datetime,
) -> dict[str, list]:
    """Fetch forecasts for all walks and compute their viable windows.

    Bounded concurrency (semaphore of 10). Walks whose fetch failed are
    absent from the returned dict, not mapped to an empty list, so the
    caller can keep their previous windows (stale beats empty).
    """
    semaphore = asyncio.Semaphore(CONCURRENCY)
    results: dict[str, list] = {}

    async def _one(walk_uuid: str, lat: float, lon: float) -> None:
        async with semaphore:
            forecast = await fetch_forecast(client, lat, lon)
        if forecast is None:
            return
        results[walk_uuid] = compute_windows(parse_hourly(forecast), now, lat, lon)

    await asyncio.gather(*(_one(u, lat, lon) for u, lat, lon in walks))
    return results
