"""Unit tests for hikes.models: round-trip Walk on SQLite.

Uses SQLModel.metadata.create_all (no migrations), mirroring the ships tests.
"""

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from hikes.models import Walk


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
    windows_updated = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
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
        windows=[[1750000000, 12.5, 0, 14, 40]],
        windows_updated_at=windows_updated,
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
    assert loaded.windows == [[1750000000, 12.5, 0, 14, 40]]
    assert isinstance(loaded.scraped_at, datetime)
    assert isinstance(loaded.created_at, datetime)
