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
from stars.models import SiteHour, SiteMonthStat


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


def _hour(time_str, sun_elevation_deg=-18.5, cloud=5.0):
    """A forecast-emitted hour dict (the shape stars.forecast.score_location
    returns and stars.jobs._write_sites consumes)."""
    return {
        "time": time_str,
        "sun_elevation_deg": sun_elevation_deg,
        "cloud_area_fraction": cloud,
        "air_temperature": 8.0,
        "dew_spread": 5.0,
        "symbol": "clearsky_night",
        "is_clear": cloud < 10.0,
    }


def _seed_hour(
    session,
    site_id,
    hour_time,
    symbol="clearsky_night",
    sun_elevation_deg=-18.5,
    cloud=5.0,
):
    session.add(
        SiteHour(
            site_id=site_id,
            hour_time=hour_time,
            cloud_area_fraction=cloud,
            air_temperature=8.0,
            dew_spread=5.0,
            sun_elevation_deg=sun_elevation_deg,
            symbol=symbol,
        )
    )


def test_write_sites_inserts_rows_for_each_site(session):
    scored = {
        "galloway-forest": [_hour("2026-06-13T23:00:00Z", cloud=5.0)],
        "tiree": [
            _hour("2026-06-13T22:00:00Z", cloud=5.0),
            _hour("2026-06-13T23:00:00Z", cloud=5.0),
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
    # The clear-dark metric inputs written by the forecast flow through.
    assert gf.sun_elevation_deg == -18.5
    assert gf.cloud_area_fraction == 5.0
    assert gf.air_temperature == 8.0
    assert gf.symbol == "clearsky_night"
    # A single shared fetched_at is stamped across the whole run.
    assert len({r.fetched_at for r in rows}) == 1


def test_write_sites_leaves_unfetched_sites_untouched(session):
    # Stale beats empty: the bulk delete only targets fetched site ids, so a
    # site absent from the scored map keeps its previous rows.
    kept = datetime(2026, 6, 13, 21, tzinfo=timezone.utc)
    _seed_hour(session, "X", kept, symbol="old")
    session.commit()

    now = datetime(2026, 6, 13, 22, tzinfo=timezone.utc)
    jobs._write_sites(session, {"A": [_hour("2026-06-13T23:00:00Z", cloud=5.0)]}, now)
    session.commit()

    survivor = session.get(SiteHour, ("X", kept))
    assert survivor is not None
    assert survivor.symbol == "old"
    assert (
        session.get(SiteHour, ("A", datetime(2026, 6, 13, 23, tzinfo=timezone.utc)))
        is not None
    )


def test_write_sites_replaces_future_only_keeps_elapsed(session):
    # ADR 008: refresh replaces a fetched site's FUTURE hours only. An elapsed
    # hour (< top_of_hour) survives the refresh so the prune can bank it; the
    # stale future hour is replaced by the freshly scored future hours.
    elapsed = datetime(2026, 6, 13, 20, tzinfo=timezone.utc)
    stale_future = datetime(2026, 6, 13, 23, tzinfo=timezone.utc)
    _seed_hour(session, "A", elapsed, symbol="banked")
    _seed_hour(session, "A", stale_future, symbol="stale")
    session.commit()

    now = datetime(2026, 6, 13, 22, tzinfo=timezone.utc)
    scored = {
        "A": [
            _hour("2026-06-13T23:00:00Z", cloud=5.0),
            _hour("2026-06-14T00:00:00Z", cloud=5.0),
        ]
    }
    jobs._write_sites(session, scored, now)
    session.commit()

    rows = session.exec(select(SiteHour).where(SiteHour.site_id == "A")).all()
    by_time = {_utc(r.hour_time): r for r in rows}
    # Elapsed row is untouched (still present, still its original symbol).
    assert elapsed in by_time
    assert by_time[elapsed].symbol == "banked"
    # Future hours are the freshly scored set; the stale 23:00 row was replaced.
    assert _utc(stale_future) in by_time
    assert by_time[_utc(stale_future)].symbol == "clearsky_night"
    assert datetime(2026, 6, 14, 0, tzinfo=timezone.utc) in by_time


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


def test_prune_banks_elapsed_into_month_stats(session):
    # Elapsed dark hours are banked into site_month_stats (the right month
    # bucket) before deletion: every elapsing row counts toward dark_hours, and
    # the clear ones (cloud < 10%) toward clear_dark_hours.
    cutoff = top_of_hour(datetime.now(timezone.utc))
    h1 = cutoff - timedelta(hours=2)
    h2 = cutoff - timedelta(hours=3)
    future = cutoff + timedelta(hours=1)
    # h1 is clear (5% cloud); h2 is dark but cloudy (50%).
    _seed_hour(session, "S", h1, cloud=5.0)
    _seed_hour(session, "S", h2, cloud=50.0)
    # A future hour must be neither banked nor deleted.
    _seed_hour(session, "S", future, cloud=5.0)
    session.commit()

    deleted = jobs._prune_elapsed(session)
    session.commit()
    assert deleted == 2

    # Group the seeded elapsed hours by month exactly as the prune does, so the
    # assertion holds even if the two hours straddle a month boundary.
    expected: dict[int, dict[str, int]] = {}
    for ht, clear in ((h1, True), (h2, False)):
        agg = expected.setdefault(ht.month, {"dark": 0, "clear": 0})
        agg["dark"] += 1
        if clear:
            agg["clear"] += 1
    for month, agg in expected.items():
        stat = session.get(SiteMonthStat, ("S", month))
        assert stat is not None
        assert stat.dark_hours == agg["dark"]
        assert stat.clear_dark_hours == agg["clear"]

    # The elapsed rows are deleted; only the future hour remains.
    remaining = {_utc(r.hour_time) for r in session.exec(select(SiteHour)).all()}
    assert remaining == {_utc(future)}


def test_prune_rerun_does_not_double_count(session):
    # The prune is the sole elapsed-remover and banks exactly once: a second run
    # over an already-pruned table banks nothing more (no double-count).
    cutoff = top_of_hour(datetime.now(timezone.utc))
    h1 = cutoff - timedelta(hours=2)
    _seed_hour(session, "S", h1, cloud=5.0)
    session.commit()

    assert jobs._prune_elapsed(session) == 1
    session.commit()
    # Rows are gone, so the second prune finds nothing to bank or delete.
    assert jobs._prune_elapsed(session) == 0
    session.commit()

    stat = session.get(SiteMonthStat, ("S", h1.month))
    assert stat is not None
    assert stat.dark_hours == 1
    assert stat.clear_dark_hours == 1


def test_prune_increments_existing_month_stat(session):
    # Banking accumulates additively into an existing month row.
    cutoff = top_of_hour(datetime.now(timezone.utc))
    h1 = cutoff - timedelta(hours=2)
    month = h1.month
    session.add(
        SiteMonthStat(
            site_id="S",
            month=month,
            dark_hours=5,
            clear_dark_hours=3,
        )
    )
    _seed_hour(session, "S", h1, cloud=5.0)
    session.commit()

    assert jobs._prune_elapsed(session) == 1
    session.commit()

    stat = session.get(SiteMonthStat, ("S", month))
    assert stat is not None
    assert stat.dark_hours == 6
    assert stat.clear_dark_hours == 4


def test_refresh_handler_empty_fetch_is_noop(monkeypatch):
    # Sites exist, but the fetch returns nothing: the handler must not write.
    async def _empty_fetch(sites):
        return {}

    called = False

    def _should_not_run(scored):  # pragma: no cover - asserted not called
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(
        jobs,
        "_load_sites",
        lambda: [{"id": "A", "lat": 57.0, "lon": -4.0, "altitude_m": 100}],
    )
    monkeypatch.setattr(jobs, "fetch_all", _empty_fetch)
    monkeypatch.setattr(jobs, "_persist_sites", _should_not_run)

    result = asyncio.run(jobs.refresh_handler(None))
    assert result is None
    assert called is False


def test_refresh_handler_no_sites_is_noop(monkeypatch):
    # No sites in stars.sites: the handler must not even attempt the fetch.
    fetched = False

    async def _should_not_fetch(sites):  # pragma: no cover - asserted not called
        nonlocal fetched
        fetched = True
        return {}

    monkeypatch.setattr(jobs, "_load_sites", lambda: [])
    monkeypatch.setattr(jobs, "fetch_all", _should_not_fetch)

    result = asyncio.run(jobs.refresh_handler(None))
    assert result is None
    assert fetched is False
