"""Unit tests for stars.models: round-trip SiteHour on SQLite.

Uses SQLModel.metadata.create_all (no migrations), mirroring the hikes tests.
"""

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from stars.models import Site, SiteHour, SiteMonthClimatology


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
        cloud_area_fraction=8.0,
        air_temperature=8.0,
        dew_spread=5.0,
        sun_elevation_deg=-18.5,
        symbol="clearsky_night",
    )
    session.add(row)
    session.commit()

    loaded = session.get(SiteHour, ("galloway-forest", hour))
    assert loaded is not None
    assert loaded.site_id == "galloway-forest"
    # The v2 metric inputs round-trip; the Q columns (score/darkness/cloud
    # factor) are gone.
    assert loaded.cloud_area_fraction == 8.0
    assert loaded.air_temperature == 8.0
    assert loaded.dew_spread == 5.0
    assert loaded.sun_elevation_deg == -18.5
    assert loaded.symbol == "clearsky_night"
    # SQLite returns naive datetimes (no tz-aware type), so assert the type
    # only, not tzinfo, matching hikes/models_test. Production is Postgres
    # TIMESTAMPTZ, where the value round-trips tz-aware.
    assert isinstance(loaded.fetched_at, datetime)


def test_site_roundtrip(session):
    row = Site(
        id="grid-0001",
        name="Grid Point 1",
        lat=57.12,
        lon=-4.70,
        altitude_m=312,
        lp_zone="1a",
        source="grid",
    )
    session.add(row)
    session.commit()

    loaded = session.get(Site, "grid-0001")
    assert loaded is not None
    assert loaded.name == "Grid Point 1"
    assert loaded.lat == 57.12
    assert loaded.lon == -4.70
    assert loaded.altitude_m == 312
    assert loaded.lp_zone == "1a"
    assert loaded.source == "grid"
    # SQLite returns naive datetimes; assert the type only, not tzinfo.
    # Production is Postgres TIMESTAMPTZ where the value round-trips tz-aware.
    assert isinstance(loaded.updated_at, datetime)


def test_site_defaults(session):
    # name nullable; altitude/lp_zone/source carry their column defaults.
    row = Site(id="grid-0002", lat=58.0, lon=-5.0)
    session.add(row)
    session.commit()

    loaded = session.get(Site, "grid-0002")
    assert loaded is not None
    assert loaded.name is None
    assert loaded.altitude_m == 0
    assert loaded.lp_zone == "unknown"
    assert loaded.source == "grid"


def test_default_symbol_is_empty(session):
    hour = datetime(2026, 6, 13, 23, 0, 0, tzinfo=timezone.utc)
    row = SiteHour(
        site_id="tiree",
        hour_time=hour,
        cloud_area_fraction=20.0,
        air_temperature=9.0,
        dew_spread=4.0,
    )
    session.add(row)
    session.commit()

    loaded = session.get(SiteHour, ("tiree", hour))
    assert loaded is not None
    assert loaded.symbol == ""
    # sun_elevation_deg and the retained vestigial weather columns carry their
    # defaults when not supplied.
    assert loaded.sun_elevation_deg == 0.0
    assert loaded.relative_humidity == 0.0
    assert loaded.wind_speed == 0.0


def test_site_month_climatology_roundtrip(session):
    row = SiteMonthClimatology(
        site_id="scotland-0001",
        month=3,
        dark_hours=300,
        clear_dark_hours=85,
    )
    session.add(row)
    session.commit()

    loaded = session.get(SiteMonthClimatology, ("scotland-0001", 3))
    assert loaded is not None
    assert loaded.dark_hours == 300
    assert loaded.clear_dark_hours == 85


def test_site_month_climatology_defaults(session):
    # dark_hours and clear_dark_hours carry their column defaults.
    row = SiteMonthClimatology(site_id="scotland-0002", month=7)
    session.add(row)
    session.commit()

    loaded = session.get(SiteMonthClimatology, ("scotland-0002", 7))
    assert loaded is not None
    assert loaded.dark_hours == 0
    assert loaded.clear_dark_hours == 0


def test_site_month_stats_table_is_gone():
    # The live bank-at-prune accumulator was retired (ADR 009): history comes
    # entirely from the climatology, so the model and its table no longer exist.
    assert "site_month_stats" not in SQLModel.metadata.tables


def test_composite_primary_key_same_site_different_hours(session):
    hour_a = datetime(2026, 6, 13, 22, 0, 0, tzinfo=timezone.utc)
    hour_b = datetime(2026, 6, 13, 23, 0, 0, tzinfo=timezone.utc)
    common = {
        "site_id": "iona",
        "cloud_area_fraction": 15.0,
        "air_temperature": 7.0,
        "dew_spread": 6.0,
    }
    session.add(SiteHour(hour_time=hour_a, **common))
    session.add(SiteHour(hour_time=hour_b, **common))
    session.commit()

    rows = session.exec(select(SiteHour).where(SiteHour.site_id == "iona")).all()
    assert len(rows) == 2
    # SQLite returns naive datetimes; coerce to UTC-aware before comparing.
    loaded = {
        r.hour_time
        if r.hour_time.tzinfo is not None
        else r.hour_time.replace(tzinfo=timezone.utc)
        for r in rows
    }
    assert loaded == {hour_a, hour_b}
