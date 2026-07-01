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
    cloud_area_fraction: float
    air_temperature: float
    dew_spread: float
    sun_elevation_deg: float = Field(default=0.0)
    # relative_humidity / wind_speed are retained NOT NULL columns from v1 that
    # the v2 clear-dark metric no longer computes (the migration drops only the
    # Q-derived score / darkness_factor / cloud_factor). They default to 0.0 so
    # inserts that omit them still satisfy the NOT NULL columns in Postgres.
    relative_humidity: float = Field(default=0.0)
    wind_speed: float = Field(default=0.0)
    symbol: str = ""
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SiteMonthClimatology(
    SQLModel, table=True
):  # nosemgrep: sqlmodel-datetime-without-factory
    """Per-site, per-month-of-year ERA5/CERRA reanalysis backfill (v2).

    A long-run seasonal baseline computed offline from the ERA5/CERRA reanalysis
    and ingested by the stars.load_climatology job from climatology.json on
    SeaweedFS. ``dark_hours`` is the per-month-of-year (1-12) count of dark hours
    (sun < -12 deg) and ``clear_dark_hours`` the subset that is also clear (cloud
    < 10%); ``clear_dark_hours / dark_hours`` is the clarity rate. /api/stars/history
    reads this table directly: it is the sole source of the historical layer now
    that the live bank-at-prune accumulator has been retired (ADR 009).
    """

    __tablename__ = "site_month_climatology"
    __table_args__ = {"schema": "stars", "extend_existing": True}

    site_id: str = Field(primary_key=True)
    month: int = Field(primary_key=True)
    dark_hours: int = Field(default=0)
    clear_dark_hours: int = Field(default=0)
