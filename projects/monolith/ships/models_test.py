"""Unit tests for ships.models: round-trip Vessel and LatestPosition on SQLite.

Uses SQLModel.metadata.create_all (no migrations), mirroring the knowledge tests.
"""

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from ships.models import HeatCellHistorical, LatestPosition, Vessel


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            yield session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


def test_vessel_roundtrip(session):
    vessel = Vessel(
        mmsi="123456789",
        imo="9074729",
        call_sign="ABCD",
        name="Ever Given",
        ship_type=70,
        dimension_a=200,
        destination="ROTTERDAM",
        draught=14.5,
    )
    session.add(vessel)
    session.commit()

    loaded = session.get(Vessel, "123456789")
    assert loaded is not None
    assert loaded.name == "Ever Given"
    assert loaded.ship_type == 70
    assert loaded.dimension_a == 200
    assert loaded.destination == "ROTTERDAM"
    assert loaded.draught == 14.5
    assert isinstance(loaded.created_at, datetime)


def test_latest_position_roundtrip(session):
    recorded = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
    pos = LatestPosition(
        mmsi="987654321",
        lat=51.95,
        lon=4.13,
        speed=12.3,
        course=270.0,
        heading=271,
        nav_status=0,
        ship_name="Maersk",
        recorded_at=recorded,
    )
    session.add(pos)
    session.commit()

    loaded = session.get(LatestPosition, "987654321")
    assert loaded is not None
    assert loaded.lat == 51.95
    assert loaded.lon == 4.13
    assert loaded.speed == 12.3
    assert loaded.ship_name == "Maersk"
    assert loaded.first_seen_at_location is None
    assert isinstance(loaded.updated_at, datetime)


def test_heat_cell_historical_roundtrip(session):
    session.add(HeatCellHistorical(lat_bin=10, lon_bin=-20, count=1234))
    session.commit()
    row = session.get(HeatCellHistorical, (10, -20))
    assert row is not None
    assert row.count == 1234
    assert isinstance(row.updated_at, datetime)
