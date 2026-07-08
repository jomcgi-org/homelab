"""SQLModel definitions for the trips schema.

Mirrors chart/migrations/20260615120000_trips_schema.sql:

- ``Trip``: one row per trip, keyed by ``slug``. Holds the display metadata
  that used to live in the frontend config.yaml (title/subtitle, the per-day
  labels, highlights and manual stats). days/highlights/stats are JSON blobs so
  the shape can evolve without a migration per field.
- ``TripPoint``: one row per map point, keyed by (trip_slug, id). Mirrors the
  old NATS TripPoint: GPS, capture time, the S3 image key, source, tags,
  elevation and the camera EXIF/optics fields. ``image`` is NULL for route-only
  "gap" points used to draw the driving line between photos.

Postgres uses JSONB / TEXT[]; the SQLite variants let SQLModel.metadata
.create_all() build the tables for the in-memory unit-test fixtures.
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Column, String
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

# Postgres-native types with SQLite fallbacks (mirrors knowledge/models.py).
_JSONB = JSONB().with_variant(JSON(), "sqlite")
_STRING_ARRAY = PG_ARRAY(String).with_variant(JSON(), "sqlite")


class Trip(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    __tablename__ = "trips"
    __table_args__ = {"schema": "trips", "extend_existing": True}

    slug: str = Field(primary_key=True)
    title: str
    short_title: str | None = None
    subtitle: str | None = None
    # IANA zone the camera-local EXIF timestamps are interpreted in when the
    # backfill converts them to the tz-aware TripPoint.taken_at. Attribute named
    # `tz` (not `timezone`) to avoid shadowing the datetime.timezone import
    # (semgrep python-shadow-module-import); the DB column stays `timezone`.
    tz: str = Field(
        default="America/Vancouver",
        sa_column=Column("timezone", String, nullable=False),
    )
    default_image: str | None = None
    default_zoom: int | None = None
    days: dict[str, Any] = Field(default_factory=dict, sa_column=Column(_JSONB))
    highlights: list[Any] = Field(default_factory=list, sa_column=Column(_JSONB))
    stats: dict[str, Any] = Field(default_factory=dict, sa_column=Column(_JSONB))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TripPoint(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    __tablename__ = "points"
    __table_args__ = {"schema": "trips", "extend_existing": True}

    trip_slug: str = Field(primary_key=True)
    id: str = Field(primary_key=True)
    lat: float
    lng: float
    taken_at: datetime
    image: str | None = None
    source: str = "gopro"
    tags: list[str] = Field(default_factory=list, sa_column=Column(_STRING_ARRAY))
    elevation: float | None = None
    light_value: float | None = None
    iso: int | None = None
    shutter_speed: str | None = None
    aperture: float | None = None
    focal_length_35mm: int | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
