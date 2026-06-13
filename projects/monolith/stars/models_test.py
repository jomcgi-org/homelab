"""Unit tests for stars.models: round-trip SiteHour on SQLite.

Uses SQLModel.metadata.create_all (no migrations), mirroring the hikes tests.
"""

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from stars.models import SiteHour


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


def test_site_hour_roundtrip(session):
    hour = datetime(2026, 6, 13, 22, 0, 0, tzinfo=timezone.utc)
    row = SiteHour(
        site_id="galloway-forest",
        hour_time=hour,
        score=87.5,
        cloud_area_fraction=10.0,
        relative_humidity=60.0,
        wind_speed=3.0,
        air_temperature=8.0,
        dew_spread=5.0,
        symbol="clearsky_night",
    )
    session.add(row)
    session.commit()

    loaded = session.get(SiteHour, ("galloway-forest", hour))
    assert loaded is not None
    assert loaded.site_id == "galloway-forest"
    assert loaded.score == 87.5
    assert loaded.cloud_area_fraction == 10.0
    assert loaded.relative_humidity == 60.0
    assert loaded.wind_speed == 3.0
    assert loaded.air_temperature == 8.0
    assert loaded.dew_spread == 5.0
    assert loaded.symbol == "clearsky_night"
    assert isinstance(loaded.fetched_at, datetime)
    assert loaded.fetched_at.tzinfo is not None
    assert loaded.fetched_at.utcoffset() is not None


def test_default_symbol_is_empty(session):
    hour = datetime(2026, 6, 13, 23, 0, 0, tzinfo=timezone.utc)
    row = SiteHour(
        site_id="tiree",
        hour_time=hour,
        score=70.0,
        cloud_area_fraction=20.0,
        relative_humidity=65.0,
        wind_speed=4.0,
        air_temperature=9.0,
        dew_spread=4.0,
    )
    session.add(row)
    session.commit()

    loaded = session.get(SiteHour, ("tiree", hour))
    assert loaded is not None
    assert loaded.symbol == ""


def test_composite_primary_key_same_site_different_hours(session):
    hour_a = datetime(2026, 6, 13, 22, 0, 0, tzinfo=timezone.utc)
    hour_b = datetime(2026, 6, 13, 23, 0, 0, tzinfo=timezone.utc)
    common = {
        "site_id": "iona",
        "score": 80.0,
        "cloud_area_fraction": 15.0,
        "relative_humidity": 62.0,
        "wind_speed": 2.0,
        "air_temperature": 7.0,
        "dew_spread": 6.0,
    }
    session.add(SiteHour(hour_time=hour_a, **common))
    session.add(SiteHour(hour_time=hour_b, **common))
    session.commit()

    rows = session.exec(select(SiteHour).where(SiteHour.site_id == "iona")).all()
    assert len(rows) == 2
    assert {r.hour_time for r in rows} == {hour_a, hour_b}
