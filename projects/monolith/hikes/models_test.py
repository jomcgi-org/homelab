"""Unit tests for hikes.models: round-trip Walk + WalkHour on SQLite.

Uses SQLModel.metadata.create_all (no migrations), mirroring the ships/stars tests.
"""

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from hikes.models import Walk, WalkHour


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


def test_walk_roundtrip(session):
    walk = Walk(
        uuid="3f2504e0-4f89-51d3-9a0c-0305e82c3301",
        name="Ben Lomond",
        url="https://www.walkhighlands.co.uk/lochlomond/ben-lomond.shtml",
        distance_km=12.0,
        ascent_m=990,
        duration_h=5.5,
        summary="A classic ascent of Scotland's most southerly Munro.",
        latitude=56.19,
        longitude=-4.633,
    )
    session.add(walk)
    session.commit()

    loaded = session.get(Walk, "3f2504e0-4f89-51d3-9a0c-0305e82c3301")
    assert loaded is not None
    assert loaded.name == "Ben Lomond"
    assert loaded.url == "https://www.walkhighlands.co.uk/lochlomond/ben-lomond.shtml"
    assert loaded.distance_km == 12.0
    assert loaded.ascent_m == 990
    assert loaded.duration_h == 5.5
    assert loaded.summary == "A classic ascent of Scotland's most southerly Munro."
    assert loaded.latitude == 56.19
    assert loaded.longitude == -4.633
    assert isinstance(loaded.scraped_at, datetime)
    assert isinstance(loaded.created_at, datetime)


def test_walk_hour_roundtrip(session):
    hour = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
    row = WalkHour(
        walk_uuid="3f2504e0-4f89-51d3-9a0c-0305e82c3301",
        hour_time=hour,
        temp_c=12.5,
        precip_mm=0.0,
        wind_kmh=14.0,
        cloud_pct=40.0,
    )
    session.add(row)
    session.commit()

    loaded = session.get(WalkHour, ("3f2504e0-4f89-51d3-9a0c-0305e82c3301", hour))
    assert loaded is not None
    assert loaded.temp_c == 12.5
    assert loaded.precip_mm == 0.0
    assert loaded.wind_kmh == 14.0
    assert loaded.cloud_pct == 40.0
    # SQLite returns naive datetimes (no tz-aware type), so assert the type
    # only, not tzinfo, matching stars/models_test. Production is Postgres
    # TIMESTAMPTZ, where the value round-trips tz-aware.
    assert isinstance(loaded.fetched_at, datetime)


def test_walk_hour_composite_key_same_walk_different_hours(session):
    hour_a = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
    hour_b = datetime(2026, 6, 13, 13, 0, 0, tzinfo=timezone.utc)
    common = {
        "walk_uuid": "3f2504e0-4f89-51d3-9a0c-0305e82c3301",
        "temp_c": 12.5,
        "precip_mm": 0.0,
        "wind_kmh": 14.0,
        "cloud_pct": 40.0,
    }
    session.add(WalkHour(hour_time=hour_a, **common))
    session.add(WalkHour(hour_time=hour_b, **common))
    session.commit()

    rows = session.exec(
        select(WalkHour).where(
            WalkHour.walk_uuid == "3f2504e0-4f89-51d3-9a0c-0305e82c3301"
        )
    ).all()
    assert len(rows) == 2
    loaded = {
        r.hour_time
        if r.hour_time.tzinfo is not None
        else r.hour_time.replace(tzinfo=timezone.utc)
        for r in rows
    }
    assert loaded == {hour_a, hour_b}
