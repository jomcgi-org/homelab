"""SQLModel definitions for the hikes schema (WalkHighlands walk corpus).

Mirrors chart/migrations/20260613000000_hikes_schema.sql (walks) and
20260613000020_hikes_walk_hours.sql (walk_hours).

- ``Walk``: one row per walk, keyed by the uuid5-of-coordinates identity from
  the original scraper.
- ``WalkHour``: one row per walk-hour, keyed by (walk_uuid, hour_time), holding
  the viable-window weather fields the forecast job computes. Replaces the old
  JSONB ``windows`` tuple-array on walks. The hourly prune job and the read
  endpoints drop hours once their clock hour ends.
"""

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


class Walk(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    __tablename__ = "walks"
    __table_args__ = {"schema": "hikes", "extend_existing": True}

    uuid: str = Field(primary_key=True)
    name: str
    url: str
    distance_km: float
    ascent_m: int
    duration_h: float
    summary: str = Field(default="")
    latitude: float
    longitude: float
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WalkHour(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    __tablename__ = "walk_hours"
    __table_args__ = {"schema": "hikes", "extend_existing": True}

    walk_uuid: str = Field(primary_key=True)
    hour_time: datetime = Field(primary_key=True)
    temp_c: float
    precip_mm: float
    wind_kmh: float
    cloud_pct: float
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
