"""Unit tests for stars.jobs.refresh_handler on SQLite.

Uses SQLModel.metadata.create_all (no migrations), mirroring stars/models_test.
fetch_all is monkeypatched so no network call is made. The async handler is
driven with asyncio.run inside a synchronous test, so no pytest-asyncio marker
or plugin dependency is required.
"""

import asyncio
from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import stars.jobs as jobs
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


def _hour(time_str, score=80.0):
    return {
        "time": time_str,
        "score": score,
        "cloud_area_fraction": 10.0,
        "relative_humidity": 60.0,
        "wind_speed": 3.0,
        "air_temperature": 8.0,
        "dew_spread": 5.0,
        "symbol": "clearsky_night",
    }


def _patch_fetch_all(monkeypatch, scored):
    async def _fake_fetch_all():
        return scored

    monkeypatch.setattr(jobs, "fetch_all", _fake_fetch_all)


def test_refresh_inserts_rows_for_each_site(monkeypatch, session):
    scored = {
        "galloway-forest": [_hour("2026-06-13T23:00:00Z", 90.0)],
        "tiree": [
            _hour("2026-06-13T22:00:00Z", 75.0),
            _hour("2026-06-13T23:00:00Z", 72.0),
        ],
    }
    _patch_fetch_all(monkeypatch, scored)

    result = asyncio.run(jobs.refresh_handler(session))
    assert result is None

    rows = session.exec(select(SiteHour)).all()
    assert len(rows) == 3

    gf = session.get(
        SiteHour,
        ("galloway-forest", datetime(2026, 6, 13, 23, tzinfo=timezone.utc)),
    )
    assert gf is not None
    assert gf.site_id == "galloway-forest"
    assert gf.score == 90.0
    assert gf.hour_time == datetime(2026, 6, 13, 23, tzinfo=timezone.utc)
    assert gf.hour_time.tzinfo is not None
    assert gf.fetched_at is not None
    assert gf.fetched_at.tzinfo is not None

    # A single shared fetched_at is stamped across the whole run.
    assert len({r.fetched_at for r in rows}) == 1


def test_refresh_empty_fetch_keeps_existing_rows(monkeypatch, session):
    hour = datetime(2026, 6, 13, 21, tzinfo=timezone.utc)
    session.add(
        SiteHour(
            site_id="X",
            hour_time=hour,
            score=88.0,
            cloud_area_fraction=5.0,
            relative_humidity=50.0,
            wind_speed=2.0,
            air_temperature=9.0,
            dew_spread=6.0,
            symbol="clearsky_night",
        )
    )
    session.commit()

    _patch_fetch_all(monkeypatch, {})

    result = asyncio.run(jobs.refresh_handler(session))
    assert result is None

    survivor = session.get(SiteHour, ("X", hour))
    assert survivor is not None
    assert survivor.score == 88.0


def test_refresh_replaces_site_rows_wholesale(monkeypatch, session):
    old_a = datetime(2026, 6, 13, 20, tzinfo=timezone.utc)
    old_b = datetime(2026, 6, 13, 21, tzinfo=timezone.utc)
    for h in (old_a, old_b):
        session.add(
            SiteHour(
                site_id="A",
                hour_time=h,
                score=50.0,
                cloud_area_fraction=40.0,
                relative_humidity=70.0,
                wind_speed=4.0,
                air_temperature=7.0,
                dew_spread=4.0,
                symbol="stale",
            )
        )
    session.commit()

    scored = {
        "A": [
            _hour("2026-06-13T23:00:00Z", 90.0),
            _hour("2026-06-14T00:00:00Z", 85.0),
        ]
    }
    _patch_fetch_all(monkeypatch, scored)

    asyncio.run(jobs.refresh_handler(session))

    rows = session.exec(select(SiteHour).where(SiteHour.site_id == "A")).all()
    assert {r.hour_time for r in rows} == {
        datetime(2026, 6, 13, 23, tzinfo=timezone.utc),
        datetime(2026, 6, 14, 0, tzinfo=timezone.utc),
    }
    # The stale rows are gone.
    assert old_a not in {r.hour_time for r in rows}
    assert all(r.symbol == "clearsky_night" for r in rows)
