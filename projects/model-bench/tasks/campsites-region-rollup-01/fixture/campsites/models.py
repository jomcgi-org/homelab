"""SQLModel tables for BC Parks campsite availability x clear-sky weather (schema 'campsites').

Mirrors chart/migrations/20260630150000_campsites_schema.sql.
"""

from __future__ import annotations

import datetime

from sqlmodel import Field, SQLModel

_SCHEMA = {"schema": "campsites", "extend_existing": True}


class Campground(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    __tablename__ = "campgrounds"
    __table_args__ = _SCHEMA
    resource_location_id: int = Field(primary_key=True)
    park_map_id: int
    name: str
    region: str = ""
    latitude: float
    longitude: float
    iana_tz: str = "America/Vancouver"
    description: str = ""
    booking_url: str = "https://camping.bcparks.ca/"
    updated_at: datetime.datetime | None = None


class Availability(
    SQLModel, table=True
):  # nosemgrep: sqlmodel-datetime-without-factory
    __tablename__ = "availability"
    __table_args__ = _SCHEMA
    # Composite primary key: one row per (campground, date).
    resource_location_id: int = Field(primary_key=True)
    date: datetime.date = Field(primary_key=True)
    has_availability: bool = False
    loops_open: int = 0
    scraped_at: datetime.datetime | None = None


class Weather(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    __tablename__ = "weather"
    __table_args__ = _SCHEMA
    # Composite primary key: one forecast row per (campground, date).
    resource_location_id: int = Field(primary_key=True)
    date: datetime.date = Field(primary_key=True)
    cloud_cover: float | None = None
    precip_sum: float | None = None
    precip_prob: int | None = None
    temp_max: float | None = None
    wind_max: float | None = None
    sunny_score: int = 0
    is_good: bool = False
    fetched_at: datetime.datetime | None = None
