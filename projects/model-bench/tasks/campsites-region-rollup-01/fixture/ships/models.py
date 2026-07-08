"""SQLModel definitions for the ships schema (AIS vessel tracking).

Mirrors chart/migrations/20260610000000_ships_schema.sql. Postgres is the single
source of truth: latest_positions is the serving + dedup read-back table, and
positions is partitioned history retained via drop-partition.
"""

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


class Vessel(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    __tablename__ = "vessels"
    __table_args__ = {"schema": "ships", "extend_existing": True}

    mmsi: str = Field(primary_key=True)
    imo: str | None = Field(default=None)
    call_sign: str | None = Field(default=None)
    name: str | None = Field(default=None)
    ship_type: int | None = Field(default=None)
    dimension_a: int | None = Field(default=None)
    dimension_b: int | None = Field(default=None)
    dimension_c: int | None = Field(default=None)
    dimension_d: int | None = Field(default=None)
    destination: str | None = Field(default=None)
    eta: datetime | None = Field(default=None)
    draught: float | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Position(SQLModel, table=True):
    __tablename__ = "positions"
    __table_args__ = {"schema": "ships", "extend_existing": True}

    # The Postgres table uses a composite PK (recorded_at, id) because it is
    # range-partitioned on recorded_at. That is a Postgres-only concern enforced
    # by the migration; the model keeps a simple single-column PK so SQLite-backed
    # unit tests (SQLModel.metadata.create_all) can create the table.
    id: int | None = Field(default=None, primary_key=True)
    mmsi: str
    lat: float
    lon: float
    speed: float | None = Field(default=None)
    course: float | None = Field(default=None)
    heading: int | None = Field(default=None)
    nav_status: int | None = Field(default=None)
    ship_name: str | None = Field(default=None)
    recorded_at: datetime
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class LatestPosition(
    SQLModel, table=True
):  # nosemgrep: sqlmodel-datetime-without-factory
    __tablename__ = "latest_positions"
    __table_args__ = {"schema": "ships", "extend_existing": True}

    mmsi: str = Field(primary_key=True)
    lat: float
    lon: float
    speed: float | None = Field(default=None)
    course: float | None = Field(default=None)
    heading: int | None = Field(default=None)
    nav_status: int | None = Field(default=None)
    ship_name: str | None = Field(default=None)
    recorded_at: datetime
    first_seen_at_location: datetime | None = Field(default=None)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class HeatCell(SQLModel, table=True):  # nosemgrep: sqlmodel-datetime-without-factory
    __tablename__ = "heat_cells"
    __table_args__ = {"schema": "ships", "extend_existing": True}

    # Precomputed traffic-density rollup: one row per occupied ~500m grid cell
    # (floor(lat/step) x floor(lon/step)) holding the count of distinct moving
    # vessels that used it. Rebuilt in full by ships.heat.heat_rollup_handler.
    lat_bin: int = Field(primary_key=True)
    lon_bin: int = Field(primary_key=True)
    count: int
    computed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class HeatCellHistorical(
    SQLModel, table=True
):  # nosemgrep: sqlmodel-datetime-without-factory
    __tablename__ = "heat_cells_historical"
    __table_args__ = {"schema": "ships", "extend_existing": True}

    # Monotonic all-time traffic accumulator: cumulative vessel-days (sum of each
    # dropped day's distinct-mover count) per ~500m cell. Banked at partition drop
    # by ships.retention; summed with the live HeatCell rollup by the serving layer.
    lat_bin: int = Field(primary_key=True)
    lon_bin: int = Field(primary_key=True)
    count: int
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
