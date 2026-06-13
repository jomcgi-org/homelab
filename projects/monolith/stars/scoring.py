"""Astronomy suitability scoring for weather forecasts.

Ported verbatim from projects/stargazer/backend/scoring.py: pure functions,
no I/O, no external deps beyond pydantic. Behaviour parity is asserted by
the corresponding scoring_test.py also ported from stargazer.
"""

from pydantic import BaseModel, Field


class WeatherData(BaseModel):
    """Weather data from MET Norway API."""

    cloud_area_fraction: float = Field(ge=0, le=100)
    relative_humidity: float = Field(ge=0, le=100)
    fog_area_fraction: float = Field(default=0, ge=0, le=100)
    wind_speed: float = Field(ge=0)
    air_temperature: float
    dew_point_temperature: float
    air_pressure_at_sea_level: float = Field(default=1013.25)


class ScoredForecast(BaseModel):
    """Forecast with astronomy suitability score."""

    time: str
    score: float = Field(ge=0, le=100)
    cloud_area_fraction: float
    relative_humidity: float
    fog_area_fraction: float
    wind_speed: float
    air_temperature: float
    dew_spread: float
    air_pressure: float
    symbol: str = ""


def _humidity_score(weather: WeatherData) -> float:
    """Relative-humidity sub-score (0-100). Lifted verbatim from the inline
    humidity branch of calculate_astronomy_score; reused by weather_modifier."""
    if weather.relative_humidity < 70:
        return 100
    if weather.relative_humidity < 85:
        return 100 - (weather.relative_humidity - 70) * 3.33
    return max(0, 50 - (weather.relative_humidity - 85) * 3.33)


def _fog_score(weather: WeatherData) -> float:
    """Fog sub-score (0-100). Lifted verbatim from the inline fog branch."""
    if weather.fog_area_fraction < 5:
        return 100
    if weather.fog_area_fraction < 20:
        return 100 - (weather.fog_area_fraction - 5) * 3.33
    return max(0, 50 - (weather.fog_area_fraction - 20) * 1.67)


def _wind_score(weather: WeatherData) -> float:
    """Wind sub-score (0-100). Lifted verbatim from the inline wind branch."""
    if weather.wind_speed < 5:
        return 100
    if weather.wind_speed < 10:
        return 100 - (weather.wind_speed - 5) * 10
    return max(0, 50 - (weather.wind_speed - 10) * 5)


def _dew_score(weather: WeatherData) -> float:
    """Dew-spread sub-score (0-100). Lifted verbatim from the inline dew branch."""
    dew_spread = weather.air_temperature - weather.dew_point_temperature
    if dew_spread > 5:
        return 100
    if dew_spread > 2:
        return 100 - (5 - dew_spread) * 16.67
    return max(0, 50 - (2 - dew_spread) * 25)


def calculate_astronomy_score(weather: WeatherData) -> float:
    """Calculate astronomy suitability score (0-100).

    Weights: cloud 50%, humidity 15%, fog 10%, wind 10%, dew 15%, pressure +0-10 bonus.
    """
    if weather.cloud_area_fraction < 20:
        cloud_score = 100
    elif weather.cloud_area_fraction < 50:
        cloud_score = 100 - (weather.cloud_area_fraction - 20) * 1.67
    else:
        cloud_score = max(0, 50 - (weather.cloud_area_fraction - 50))

    humidity_score = _humidity_score(weather)
    fog_score = _fog_score(weather)
    wind_score = _wind_score(weather)
    dew_score = _dew_score(weather)

    pressure_bonus = 0
    if weather.air_pressure_at_sea_level > 1015:
        pressure_bonus = min(10, (weather.air_pressure_at_sea_level - 1015) * 2)

    weighted = (
        cloud_score * 0.50
        + humidity_score * 0.15
        + fog_score * 0.10
        + wind_score * 0.10
        + dew_score * 0.15
        + pressure_bonus
    )
    return min(100, max(0, weighted))


CIVIL_TWILIGHT_DEG = -6.0
ASTRONOMICAL_DEG = -18.0
CLOUD_FALLOFF_SPAN = 45.0  # cloud % beyond the darkness-scaled allowance at which the cloud factor reaches 0


def darkness_factor(sun_elevation_deg: float) -> float:
    """Continuous darkness 0..1: 0 at civil twilight (-6 deg), 1 at astronomical
    darkness (-18 deg); nautical (-12 deg) lands at 0.5 (ADR 007)."""
    if sun_elevation_deg >= CIVIL_TWILIGHT_DEG:
        return 0.0
    if sun_elevation_deg <= ASTRONOMICAL_DEG:
        return 1.0
    return (CIVIL_TWILIGHT_DEG - sun_elevation_deg) / (
        CIVIL_TWILIGHT_DEG - ASTRONOMICAL_DEG
    )


def cloud_factor(cloud_area_fraction: float, darkness: float) -> float:
    """Cloud 0..1 with a darkness-scaled allowance: full credit up to ~5% cloud
    when only ok-dark and ~10% when very dark, then linear falloff (ADR 007).
    Deeper darkness forgives more cloud."""
    allowance = 5.0 + 5.0 * darkness
    excess = max(0.0, cloud_area_fraction - allowance)
    return max(0.0, 1.0 - excess / CLOUD_FALLOFF_SPAN)


def weather_modifier(weather: WeatherData) -> float:
    """Non-cloud weather (humidity, fog, wind, dew) folded into a 0.7..1.0
    modifier so it nudges quality without overpowering the darkness/cloud core.
    Reuses the same per-component sub-scores as calculate_astronomy_score."""
    avg = (
        _humidity_score(weather)
        + _fog_score(weather)
        + _wind_score(weather)
        + _dew_score(weather)
    ) / 4.0  # each is 0..100
    return 0.7 + 0.3 * (avg / 100.0)


def quality_score(weather: WeatherData, sun_elevation_deg: float) -> float:
    """Continuous stargazing quality 0..100 = D x C x W (ADR 007). 0 when the
    sky is not at least civil-dark."""
    d = darkness_factor(sun_elevation_deg)
    if d <= 0.0:
        return 0.0
    c = cloud_factor(weather.cloud_area_fraction, d)
    w = weather_modifier(weather)
    return d * c * w * 100.0


def is_dark_enough(
    sun_altitude: float,
    astronomical_darkness_threshold: float = -18.0,
) -> bool:
    """Astronomical darkness: sun > 18° below horizon."""
    return sun_altitude <= astronomical_darkness_threshold
