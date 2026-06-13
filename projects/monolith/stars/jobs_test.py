"""Unit tests for stars.jobs DB cores on SQLite.

Uses SQLModel.metadata.create_all (no migrations), mirroring stars/models_test.
The async handlers delegate their DB work to synchronous cores (_write_sites,
_prune_elapsed) via asyncio.to_thread with a fresh engine session, mirroring
hikes.jobs / ships.retention. The cores take an explicit session so they are
testable against the in-memory fixture; the thin async wrappers are not unit
tested here (only the empty-fetch guard is, by monkeypatching the persist step).
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import stars.jobs as jobs
from shared.forecast_freshness import top_of_hour
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


def _utc(dt):
    """Coerce a loaded datetime to UTC-aware (SQLite returns naive values)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


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


def _seed_hour(session, site_id, hour_time, symbol="clearsky_night", score=70.0):
    session.add(
        SiteHour(
            site_id=site_id,
            hour_time=hour_time,
            score=score,
            cloud_area_fraction=10.0,
            relative_humidity=60.0,
            wind_speed=3.0,
            air_temperature=8.0,
            dew_spread=5.0,
            symbol=symbol,
        )
    )


def test_write_sites_inserts_rows_for_each_site(session):
    scored = {
        "galloway-forest": [_hour("2026-06-13T23:00:00Z", 90.0)],
        "tiree": [
            _hour("2026-06-13T22:00:00Z", 75.0),
            _hour("2026-06-13T23:00:00Z", 72.0),
        ],
    }
    now = datetime(2026, 6, 13, 21, tzinfo=timezone.utc)

    written = jobs._write_sites(session, scored, now)
    session.commit()
    assert written == 3

    rows = session.exec(select(SiteHour)).all()
    assert len(rows) == 3

    gf = session.get(
        SiteHour,
        ("galloway-forest", datetime(2026, 6, 13, 23, tzinfo=timezone.utc)),
    )
    assert gf is not None
    assert gf.score == 90.0
    # A single shared fetched_at is stamped across the whole run.
    assert len({r.fetched_at for r in rows}) == 1


def test_write_sites_leaves_unfetched_sites_untouched(session):
    # Stale beats empty: the bulk delete only targets fetched site ids, so a
    # site absent from the scored map keeps its previous rows.
    kept = datetime(2026, 6, 13, 21, tzinfo=timezone.utc)
    _seed_hour(session, "X", kept, symbol="old", score=88.0)
    session.commit()

    now = datetime(2026, 6, 13, 22, tzinfo=timezone.utc)
    jobs._write_sites(session, {"A": [_hour("2026-06-13T23:00:00Z", 90.0)]}, now)
    session.commit()

    survivor = session.get(SiteHour, ("X", kept))
    assert survivor is not None
    assert survivor.score == 88.0
    assert (
        session.get(SiteHour, ("A", datetime(2026, 6, 13, 23, tzinfo=timezone.utc)))
        is not None
    )


def test_write_sites_replaces_site_rows_wholesale(session):
    old_a = datetime(2026, 6, 13, 20, tzinfo=timezone.utc)
    old_b = datetime(2026, 6, 13, 21, tzinfo=timezone.utc)
    _seed_hour(session, "A", old_a, symbol="stale", score=50.0)
    _seed_hour(session, "A", old_b, symbol="stale", score=50.0)
    session.commit()

    now = datetime(2026, 6, 13, 22, tzinfo=timezone.utc)
    scored = {
        "A": [
            _hour("2026-06-13T23:00:00Z", 90.0),
            _hour("2026-06-14T00:00:00Z", 85.0),
        ]
    }
    jobs._write_sites(session, scored, now)
    session.commit()

    rows = session.exec(select(SiteHour).where(SiteHour.site_id == "A")).all()
    assert {_utc(r.hour_time) for r in rows} == {
        datetime(2026, 6, 13, 23, tzinfo=timezone.utc),
        datetime(2026, 6, 14, 0, tzinfo=timezone.utc),
    }
    assert all(r.symbol == "clearsky_night" for r in rows)


def test_prune_elapsed_drops_only_elapsed_hours(session):
    cutoff = top_of_hour(datetime.now(timezone.utc))
    past_a = cutoff - timedelta(hours=3)
    past_b = cutoff - timedelta(hours=1)
    current = cutoff
    future = cutoff + timedelta(hours=2)
    for i, h in enumerate((past_a, past_b, current, future)):
        _seed_hour(session, f"site-{i}", h)
    session.commit()

    deleted = jobs._prune_elapsed(session)
    session.commit()
    assert deleted == 2

    remaining = {_utc(r.hour_time) for r in session.exec(select(SiteHour)).all()}
    assert remaining == {current, future}


def test_refresh_handler_empty_fetch_is_noop(monkeypatch):
    # When the fetch returns nothing, the handler must not touch the DB at all.
    async def _empty_fetch():
        return {}

    called = False

    def _should_not_run(scored):  # pragma: no cover - asserted not called
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(jobs, "fetch_all", _empty_fetch)
    monkeypatch.setattr(jobs, "_persist_sites", _should_not_run)

    result = asyncio.run(jobs.refresh_handler(None))
    assert result is None
    assert called is False
