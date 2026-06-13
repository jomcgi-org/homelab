"""SQLModel definitions for the stars schema (dark-sky viewing windows).

Mirrors chart/migrations/20260613000010_stars_schema.sql (site_hours) and
20260613000040_stars_sites.sql (sites). One site_hours row per site-hour, keyed
by (site_id, hour_time). Static site metadata (name/lat/lon/altitude/lp_zone)
lives in the stars.sites table (sourced from the light-pollution grid, ADR 006)
and is joined in at read time. The hourly prune job and the read endpoint both
drop hours once their clock hour ends.
"""

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


class Site(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    __tablename__ = "sites"
    __table_args__ = {"schema": "stars", "extend_existing": True}

    id: str = Field(primary_key=True)
    name: str | None = None
    lat: float
    lon: float
    altitude_m: int = Field(default=0)
    lp_zone: str = Field(default="unknown")
    source: str = Field(default="grid")
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SiteHour(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    __tablename__ = "site_hours"
    __table_args__ = {"schema": "stars", "extend_existing": True}

    site_id: str = Field(primary_key=True)
    hour_time: datetime = Field(primary_key=True)
    score: float
    cloud_area_fraction: float
    relative_humidity: float
    wind_speed: float
    air_temperature: float
    dew_spread: float
    sun_elevation_deg: float = Field(default=0.0)
    symbol: str = ""
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
