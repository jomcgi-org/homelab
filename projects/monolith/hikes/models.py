"""SQLModel definitions for the hikes schema (WalkHighlands walk corpus).

Mirrors chart/migrations/20260613000000_hikes_schema.sql. One row per walk,
keyed by the uuid5-of-coordinates identity from the original scraper. The
windows column holds compact viable-window tuples
([timestamp, temp_c, precip_mm, wind_kmh, cloud_pct]) replaced wholesale by
the forecast job.
"""

from datetime import datetime, timezone

from sqlalchemy import JSON, Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

# Postgres uses JSONB (matching the migration); SQLite falls back to JSON so
# the in-memory test fixture can create the table.
_JSONB = JSONB().with_variant(JSON(), "sqlite")


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
    windows: list = Field(default_factory=list, sa_column=Column(_JSONB))
    windows_updated_at: datetime | None = Field(default=None)
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
