"""Unit tests for trips.models: round-trip Trip + TripPoint on SQLite.

Uses SQLModel.metadata.create_all (no migrations), mirroring the hikes/ships/
stars model tests. The schema-stripping fixture lets the schema-qualified
tables build on SQLite.
"""

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from trips.models import Trip, TripPoint


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


def test_trip_roundtrip(session):
    trip = Trip(
        slug="2025-liard-hot-springs",
        title="WINTER ROAD TRIP",
        short_title="Liard",
        subtitle="Vancouver -> Yukon -> Vancouver",
        default_image="img_2d30f3c65619",
        default_zoom=4,
        days={"1": {"label": "VAN -> KAMLOOPS", "notes": "The beginning"}},
        highlights=[{"id": "liard-hotsprings", "type": "hotspring", "day": 6}],
        stats={"coldest_temp": -34},
    )
    session.add(trip)
    session.commit()

    loaded = session.get(Trip, "2025-liard-hot-springs")
    assert loaded is not None
    assert loaded.title == "WINTER ROAD TRIP"
    assert loaded.short_title == "Liard"
    # Default timezone applies when not set explicitly.
    assert loaded.timezone == "America/Vancouver"
    assert loaded.default_zoom == 4
    assert loaded.days["1"]["label"] == "VAN -> KAMLOOPS"
    assert loaded.highlights[0]["id"] == "liard-hotsprings"
    assert loaded.stats["coldest_temp"] == -34
    assert isinstance(loaded.created_at, datetime)


def test_trip_point_roundtrip_with_optics(session):
    taken = datetime(2025, 1, 3, 18, 30, 0, tzinfo=timezone.utc)
    point = TripPoint(
        trip_slug="2025-liard-hot-springs",
        id="2d30f3c65619",
        lat=50.6745,
        lng=-120.3273,
        taken_at=taken,
        image="img_2d30f3c65619.jpg",
        source="camera",
        tags=["car", "hotspring"],
        elevation=345.0,
        light_value=8.6,
        iso=393,
        shutter_speed="1/240",
        aperture=2.5,
        focal_length_35mm=16,
    )
    session.add(point)
    session.commit()

    loaded = session.get(TripPoint, ("2025-liard-hot-springs", "2d30f3c65619"))
    assert loaded is not None
    assert loaded.lat == 50.6745
    assert loaded.lng == -120.3273
    assert loaded.image == "img_2d30f3c65619.jpg"
    assert loaded.source == "camera"
    assert loaded.tags == ["car", "hotspring"]
    assert loaded.elevation == 345.0
    assert loaded.light_value == 8.6
    assert loaded.iso == 393
    assert loaded.shutter_speed == "1/240"
    assert loaded.aperture == 2.5
    assert loaded.focal_length_35mm == 16
    # SQLite returns naive datetimes; production is Postgres TIMESTAMPTZ.
    assert isinstance(loaded.taken_at, datetime)


def test_trip_point_gap_has_no_image(session):
    gap = TripPoint(
        trip_slug="2025-liard-hot-springs",
        id="gap_aabbccddeeff",
        lat=51.0,
        lng=-121.0,
        taken_at=datetime(2025, 1, 3, 10, 28, 0, tzinfo=timezone.utc),
        source="gap",
        tags=["gap", "car"],
    )
    session.add(gap)
    session.commit()

    rows = session.exec(select(TripPoint).where(TripPoint.source == "gap")).all()
    assert len(rows) == 1
    assert rows[0].image is None
    assert rows[0].elevation is None
    assert rows[0].tags == ["gap", "car"]
