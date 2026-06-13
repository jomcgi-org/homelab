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

    if weather.relative_humidity < 70:
        humidity_score = 100
    elif weather.relative_humidity < 85:
        humidity_score = 100 - (weather.relative_humidity - 70) * 3.33
    else:
        humidity_score = max(0, 50 - (weather.relative_humidity - 85) * 3.33)

    if weather.fog_area_fraction < 5:
        fog_score = 100
    elif weather.fog_area_fraction < 20:
        fog_score = 100 - (weather.fog_area_fraction - 5) * 3.33
    else:
        fog_score = max(0, 50 - (weather.fog_area_fraction - 20) * 1.67)

    if weather.wind_speed < 5:
        wind_score = 100
    elif weather.wind_speed < 10:
        wind_score = 100 - (weather.wind_speed - 5) * 10
    else:
        wind_score = max(0, 50 - (weather.wind_speed - 10) * 5)

    dew_spread = weather.air_temperature - weather.dew_point_temperature
    if dew_spread > 5:
        dew_score = 100
    elif dew_spread > 2:
        dew_score = 100 - (5 - dew_spread) * 16.67
    else:
        dew_score = max(0, 50 - (2 - dew_spread) * 25)

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


def is_dark_enough(
    sun_altitude: float,
    astronomical_darkness_threshold: float = -18.0,
) -> bool:
    """Astronomical darkness: sun > 18° below horizon."""
    return sun_altitude <= astronomical_darkness_threshold
