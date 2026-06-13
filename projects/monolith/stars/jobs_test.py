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


def _hour(time_str, score=80.0, darkness=1.0, clarity=0.9):
    return {
        "time": time_str,
        "score": score,
        "sun_elevation_deg": -18.5,
        "darkness_factor": darkness,
        "cloud_factor": clarity,
        "cloud_area_fraction": 10.0,
        "relative_humidity": 60.0,
        "wind_speed": 3.0,
        "air_temperature": 8.0,
        "dew_spread": 5.0,
        "symbol": "clearsky_night",
    }


def _seed_hour(
    session,
    site_id,
    hour_time,
    symbol="clearsky_night",
    score=70.0,
    darkness=0.0,
    clarity=0.0,
):
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
            darkness_factor=darkness,
            cloud_factor=clarity,
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
    # The sun elevation written by the forecast flows through to the row.
    assert gf.sun_elevation_deg == -18.5
    # The decomposed factors flow through so the prune can bank them.
    assert gf.darkness_factor == 1.0
    assert gf.cloud_factor == 0.9
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


def test_write_sites_replaces_future_only_keeps_elapsed(session):
    # ADR 008: refresh replaces a fetched site's FUTURE hours only. An elapsed
    # hour (< top_of_hour) survives the refresh so the prune can bank it; the
    # stale future hour is replaced by the freshly scored future hours.
    elapsed = datetime(2026, 6, 13, 20, tzinfo=timezone.utc)
    stale_future = datetime(2026, 6, 13, 23, tzinfo=timezone.utc)
    _seed_hour(session, "A", elapsed, symbol="banked", score=50.0)
    _seed_hour(session, "A", stale_future, symbol="stale", score=50.0)
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
    by_time = {_utc(r.hour_time): r for r in rows}
    # Elapsed row is untouched (still present, still its original symbol/score).
    assert elapsed in by_time
    assert by_time[elapsed].symbol == "banked"
    assert by_time[elapsed].score == 50.0
    # Future hours are the freshly scored set; the stale 23:00 row was replaced.
    assert _utc(stale_future) in by_time
    assert by_time[_utc(stale_future)].symbol == "clearsky_night"
    assert by_time[_utc(stale_future)].score == 90.0
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
    # ADR 008: elapsed hours are banked into site_month_stats (the right month
    # bucket, summed score/darkness/clarity, counted) before being deleted.
    cutoff = top_of_hour(datetime.now(timezone.utc))
    h1 = cutoff - timedelta(hours=2)
    h2 = cutoff - timedelta(hours=3)
    future = cutoff + timedelta(hours=1)
    _seed_hour(session, "S", h1, score=80.0, darkness=1.0, clarity=0.9)
    _seed_hour(session, "S", h2, score=60.0, darkness=0.5, clarity=0.8)
    # A future hour must be neither banked nor deleted.
    _seed_hour(session, "S", future, score=99.0, darkness=1.0, clarity=1.0)
    session.commit()

    deleted = jobs._prune_elapsed(session)
    session.commit()
    assert deleted == 2

    # Group the seeded elapsed hours by month exactly as the prune does, so the
    # assertion holds even if the two hours straddle a month boundary.
    expected: dict[int, dict[str, float]] = {}
    for ht, score, dark, clarity in ((h1, 80.0, 1.0, 0.9), (h2, 60.0, 0.5, 0.8)):
        agg = expected.setdefault(ht.month, {"n": 0, "q": 0.0, "d": 0.0, "c": 0.0})
        agg["n"] += 1
        agg["q"] += score
        agg["d"] += dark
        agg["c"] += clarity
    for month, agg in expected.items():
        stat = session.get(SiteMonthStat, ("S", month))
        assert stat is not None
        assert stat.window_count == agg["n"]
        assert stat.sum_q == pytest.approx(agg["q"])
        assert stat.sum_darkness == pytest.approx(agg["d"])
        assert stat.sum_clarity == pytest.approx(agg["c"])

    # The elapsed rows are deleted; only the future hour remains.
    remaining = {_utc(r.hour_time) for r in session.exec(select(SiteHour)).all()}
    assert remaining == {_utc(future)}


def test_prune_rerun_does_not_double_count(session):
    # The prune is the sole elapsed-remover and banks exactly once: a second run
    # over an already-pruned table banks nothing more (no double-count).
    cutoff = top_of_hour(datetime.now(timezone.utc))
    h1 = cutoff - timedelta(hours=2)
    _seed_hour(session, "S", h1, score=80.0, darkness=1.0, clarity=0.9)
    session.commit()

    assert jobs._prune_elapsed(session) == 1
    session.commit()
    # Rows are gone, so the second prune finds nothing to bank or delete.
    assert jobs._prune_elapsed(session) == 0
    session.commit()

    stat = session.get(SiteMonthStat, ("S", h1.month))
    assert stat is not None
    assert stat.window_count == 1
    assert stat.sum_q == pytest.approx(80.0)


def test_prune_increments_existing_month_stat(session):
    # Banking accumulates additively into an existing month row.
    cutoff = top_of_hour(datetime.now(timezone.utc))
    h1 = cutoff - timedelta(hours=2)
    month = h1.month
    session.add(
        SiteMonthStat(
            site_id="S",
            month=month,
            window_count=5,
            sum_q=400.0,
            sum_darkness=4.0,
            sum_clarity=3.5,
        )
    )
    _seed_hour(session, "S", h1, score=80.0, darkness=1.0, clarity=0.9)
    session.commit()

    assert jobs._prune_elapsed(session) == 1
    session.commit()

    stat = session.get(SiteMonthStat, ("S", month))
    assert stat is not None
    assert stat.window_count == 6
    assert stat.sum_q == pytest.approx(480.0)
    assert stat.sum_darkness == pytest.approx(5.0)
    assert stat.sum_clarity == pytest.approx(4.4)


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
