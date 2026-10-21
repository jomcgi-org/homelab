"""met.no forecast fetch and viable-window computation.

Ported from projects/hikes/update_forecast/update.py with fetch split from
the pure logic: fetch_forecast hits locationforecast/2.0/complete, parse_hourly
flattens the timeseries, compute_hours keeps every future daylight hour
(past hours, 7-day horizon, real per-coordinate daylight gate) as an hour
tuple (see HOUR_FIELDS), and compute_windows applies the viability rule
(precip > 2.0 mm or wind > 80 km/h is not viable) and emits the compact
window tuples [timestamp, temp_c, precip_mm, wind_kmh, cloud_pct] with the
original rounding rules so the windows match the old bundle format exactly.

Every daylight hour is stored, good or not, so the Scotland connector can
judge an hour by the caller's own limits; the hikes app filters on
is_viable when it reads. Wind is stored in km/h to two decimals:
met.no gives m/s (and rain in mm) to one decimal, so the stored value is
exact and the read filter matches the raw viability rule at the 80 km/h
boundary. A finer input would be rounded before the rule applies.

The complete forecast adds fog and low cloud, which met.no gives across
Scotland. Its gusts, rain range and chances of rain and thunder are left
out: they come from met.no's Nordic model, which in Scotland covers
Shetland only.

The one intentional change from the legacy logic: the daylight gate. The old
code kept a fixed 07:00-19:00 UTC band, which is wrong both ways in Scotland
(it drops viable ~03:30-07:00 / 19:00-21:00 UTC summer hours and admits dark
winter hours). We now gate on the walk's actual sunrise/sunset for that UTC
date via the NOAA sunrise equation (see sun_times). met.no timestamps are
already UTC, so the computed sun times are UTC too and compare directly.
"""

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger("monolith.hikes")

FORECAST_URL = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
# met.no requires an identifying User-Agent.
USER_AGENT = "jomcgi.dev/app/hikes (https://github.com/jomcgi/homelab)"
TIMEOUT_SECS = 30.0
# met.no asks for at most 20 req/s; the old code used 20 workers. Stay below.
CONCURRENCY = 10

MAX_PRECIPITATION_MM = 2.0
MAX_WIND_KMH = 80.0
FORECAST_HORIZON_DAYS = 7

# Earth's axial tilt (obliquity of the ecliptic), degrees.
_OBLIQUITY_DEG = 23.4397
# Standard sunrise/sunset altitude of the sun's centre: -0.833 deg accounts for
# atmospheric refraction (~34') plus the solar disc's apparent radius (~16').
_SUN_ALTITUDE_DEG = -0.833


def sun_times(
    date: datetime, lat: float, lon: float
) -> tuple[datetime, datetime] | None:
    """Return (sunrise_utc, sunset_utc) for the UTC calendar day of ``date`` at
    (lat, lon), via the NOAA sunrise equation. Pure; accurate to ~1 minute.

    Returns None when the sun never rises that day (polar night). For the
    polar-day case (sun never sets) returns the full UTC day [00:00, 24:00) so
    every hour reads as daylight. Scotland (~56-58 N) never hits either branch;
    they are defensive so the gate degrades sanely anywhere on Earth.
    """
    day_start = datetime(date.year, date.month, date.day, tzinfo=timezone.utc)

    # Julian day count since the J2000.0 epoch (2000-01-01 12:00 UTC), anchored
    # at noon of the target day. unix / 86400 + 2440587.5 is the Julian Date.
    noon = day_start + timedelta(hours=12)
    julian_day = noon.timestamp() / 86400.0 + 2440587.5
    n = julian_day - 2451545.0 + 0.0008

    mean_solar_noon = n - lon / 360.0
    solar_anomaly = math.radians((357.5291 + 0.98560028 * mean_solar_noon) % 360.0)
    center = (
        1.9148 * math.sin(solar_anomaly)
        + 0.0200 * math.sin(2 * solar_anomaly)
        + 0.0003 * math.sin(3 * solar_anomaly)
    )
    ecliptic_lon = math.radians(
        (math.degrees(solar_anomaly) + center + 180.0 + 102.9372) % 360.0
    )

    solar_transit = (
        2451545.0
        + mean_solar_noon
        + 0.0053 * math.sin(solar_anomaly)
        - 0.0069 * math.sin(2 * ecliptic_lon)
    )
    declination = math.asin(
        math.sin(ecliptic_lon) * math.sin(math.radians(_OBLIQUITY_DEG))
    )

    lat_rad = math.radians(lat)
    cos_hour_angle = (
        math.sin(math.radians(_SUN_ALTITUDE_DEG))
        - math.sin(lat_rad) * math.sin(declination)
    ) / (math.cos(lat_rad) * math.cos(declination))

    if cos_hour_angle >= 1.0:
        # Sun stays below the horizon all day: polar night, no daylight.
        return None
    if cos_hour_angle <= -1.0:
        # Sun never sets: polar day. Treat the whole UTC day as daylight.
        return day_start, day_start + timedelta(days=1)

    hour_angle = math.degrees(math.acos(cos_hour_angle))
    sunrise_jd = solar_transit - hour_angle / 360.0
    sunset_jd = solar_transit + hour_angle / 360.0

    sunrise = datetime.fromtimestamp(
        (sunrise_jd - 2440587.5) * 86400.0, tz=timezone.utc
    )
    sunset = datetime.fromtimestamp((sunset_jd - 2440587.5) * 86400.0, tz=timezone.utc)
    return sunrise, sunset


def is_daylight(dt: datetime, lat: float, lon: float) -> bool:
    """True if ``dt`` (UTC) falls between sunrise and sunset at (lat, lon).

    Replaces the legacy fixed 07:00-19:00 UTC band. An hour counts as daylight
    when its start instant is within [sunrise, sunset]; polar night is never
    daylight, polar day always is (see sun_times).
    """
    times = sun_times(dt, lat, lon)
    if times is None:
        return False
    sunrise, sunset = times
    return sunrise <= dt <= sunset


async def fetch_forecast(
    client: httpx.AsyncClient, lat: float, lon: float
) -> dict | None:
    """Fetch the complete location forecast for one coordinate pair.

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

        next_details = next_1_hours.get("details", {})
        hourly_data.append(
            {
                "time": time_str,
                "temp_c": instant.get("air_temperature"),
                "wind_speed_ms": instant.get("wind_speed"),
                # Coerce a present-but-null precipitation_amount to 0 so the
                # downstream `> threshold` comparison never sees None.
                "precipitation_mm": next_details.get("precipitation_amount", 0) or 0,
                "cloud_area_fraction": instant.get("cloud_area_fraction"),
                # The complete forecast's extras; None when absent.
                "fog_pct": instant.get("fog_area_fraction"),
                "low_cloud_pct": instant.get("cloud_area_fraction_low"),
            }
        )

    return hourly_data


def is_viable(precip_mm: float, wind_kmh: float) -> bool:
    """The viability rule on stored values (see the module docstring)."""
    return precip_mm <= MAX_PRECIPITATION_MM and wind_kmh <= MAX_WIND_KMH


# compute_hours tuple layout. The first five match the window tuple; the
# rest come from the complete forecast and are None when absent.
HOUR_FIELDS = (
    "ts",
    "temp_c",
    "precip_mm",
    "wind_kmh",
    "cloud_pct",
    "fog_pct",
    "low_cloud_pct",
)


def _kmh(ms: float | None) -> float | None:
    """m/s to km/h, two decimals: exact for met.no's one-decimal m/s."""
    return None if ms is None else round(ms * 3.6, 2)


def _opt(value: float | None, ndigits: int = 0) -> float | None:
    if value is None:
        return None
    return round(value, ndigits) if ndigits else round(value)


def compute_hours(
    hourly: list[dict], now: datetime, lat: float, lon: float
) -> list[list]:
    """Every future daylight hour of the forecast, good or not. Pure.

    Filter ladder: skip past hours, skip beyond now + 7 days, skip hours
    outside the walk's real daylight (sunrise to sunset at lat/lon for that UTC
    date, via is_daylight). Emits HOUR_FIELDS tuples with the original
    rounding and None-default rules for the first five fields, except wind,
    which is km/h to two decimals so is_viable on the stored value equals the
    raw rule.
    """
    horizon = now + timedelta(days=FORECAST_HORIZON_DAYS)
    hours: list[list] = []

    for weather in hourly:
        dt = datetime.fromisoformat(weather["time"])

        if dt < now:
            continue
        if dt > horizon:
            continue
        if not is_daylight(dt, lat, lon):
            continue

        temp_c = round(weather["temp_c"], 1) if weather["temp_c"] is not None else 0
        precip_mm = (
            round(weather["precipitation_mm"], 1)
            if weather["precipitation_mm"] > 0
            else 0
        )
        cloud_pct = (
            round(weather["cloud_area_fraction"])
            if weather["cloud_area_fraction"] is not None
            else 50
        )
        hours.append(
            [
                int(dt.timestamp()),
                temp_c,
                precip_mm,
                _kmh(weather["wind_speed_ms"] or 0),
                cloud_pct,
                _opt(weather.get("fog_pct")),
                _opt(weather.get("low_cloud_pct")),
            ]
        )

    return hours


def compute_windows(
    hourly: list[dict], now: datetime, lat: float, lon: float
) -> list[list]:
    """Compute viable hiking windows from hourly forecast data. Pure.

    compute_hours' daylight hours that pass is_viable, as the legacy
    [timestamp, temp_c, precip_mm, wind_kmh, cloud_pct] tuple with wind
    rounded to whole km/h. The daylight gate is the one deliberate divergence
    from the legacy fixed 07:00-19:00 band (see the module docstring).
    """
    return [
        [h[0], h[1], h[2], round(h[3]), h[4]]
        for h in compute_hours(hourly, now, lat, lon)
        if is_viable(h[2], h[3])
    ]


async def fetch_all_windows(
    client: httpx.AsyncClient,
    walks: list[tuple[str, float, float]],
    now: datetime,
) -> dict[str, list]:
    """Fetch forecasts for all walks and compute their daylight hours.

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
        try:
            windows = compute_hours(parse_hourly(forecast), now, lat, lon)
        except Exception:
            # A single malformed forecast must not abort the whole refresh;
            # this walk stays absent from results and keeps its prior windows.
            logger.warning(
                "hikes forecast: window computation failed for (%s, %s)",
                lat,
                lon,
                exc_info=True,
            )
            return
        results[walk_uuid] = windows

    await asyncio.gather(*(_one(u, lat, lon) for u, lat, lon in walks))
    return results
