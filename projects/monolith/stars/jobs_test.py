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
    # Refresh replaces a fetched site's FUTURE hours only. An elapsed hour
    # (< top_of_hour) survives the refresh and is left for the prune to delete;
    # the stale future hour is replaced by the freshly scored future hours.
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


def test_prune_only_deletes_no_banking(session):
    # The prune is pure housekeeping now: it deletes elapsed hours and writes no
    # accumulator. The live bank-at-prune table (site_month_stats) was retired in
    # favour of the ERA5/CERRA climatology (ADR 009), so the model is gone and the
    # SQLite fixture never creates that table.
    assert "site_month_stats" not in SQLModel.metadata.tables

    cutoff = top_of_hour(datetime.now(timezone.utc))
    h1 = cutoff - timedelta(hours=2)
    h2 = cutoff - timedelta(hours=3)
    future = cutoff + timedelta(hours=1)
    _seed_hour(session, "S", h1, cloud=5.0)
    _seed_hour(session, "S", h2, cloud=50.0)
    _seed_hour(session, "S", future, cloud=5.0)
    session.commit()

    before = len(session.exec(select(SiteHour)).all())
    deleted = jobs._prune_elapsed(session)
    session.commit()
    assert deleted == 2

    # The elapsed rows are deleted; only the future hour remains. The count drops
    # by exactly the deleted rows, with nothing banked anywhere.
    remaining = session.exec(select(SiteHour)).all()
    assert len(remaining) == before - deleted == 1
    assert {_utc(r.hour_time) for r in remaining} == {_utc(future)}


def test_prune_rerun_on_empty_table_is_noop(session):
    # A second run over an already-pruned table deletes nothing more.
    cutoff = top_of_hour(datetime.now(timezone.utc))
    h1 = cutoff - timedelta(hours=2)
    _seed_hour(session, "S", h1, cloud=5.0)
    session.commit()

    assert jobs._prune_elapsed(session) == 1
    session.commit()
    assert jobs._prune_elapsed(session) == 0
    session.commit()
    assert session.exec(select(SiteHour)).all() == []


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


def test_chunks_covers_all_items_without_overlap():
    # Batching must partition the site list exactly: every item once, last short.
    items = list(range(7))
    batches = list(jobs._chunks(items, 3))
    assert batches == [[0, 1, 2], [3, 4, 5], [6]]
    assert sum(batches, []) == items


def test_chunks_empty_input_yields_nothing():
    assert list(jobs._chunks([], 3)) == []


def test_refresh_handler_processes_all_sites_in_batches(monkeypatch):
    # More sites than the batch size: the handler fetches + persists in bounded
    # batches, covering every site exactly once and summing the totals. This is
    # the OOM fix: fetch_all is never called with the whole grid at once.
    sites = [
        {"id": f"s{i}", "lat": 57.0, "lon": -4.0, "altitude_m": 100} for i in range(5)
    ]
    monkeypatch.setattr(jobs, "_load_sites", lambda: sites)
    monkeypatch.setattr(jobs, "REFRESH_BATCH_SIZE", 2)

    fetched_batches = []

    async def _fake_fetch(batch):
        fetched_batches.append([s["id"] for s in batch])
        return {s["id"]: [_hour("2026-06-13T23:00:00Z")] for s in batch}

    persisted = []

    def _fake_persist(scored):
        persisted.append(sorted(scored))
        return len(scored)  # one hour per site in this batch

    monkeypatch.setattr(jobs, "fetch_all", _fake_fetch)
    monkeypatch.setattr(jobs, "_persist_sites", _fake_persist)

    result = asyncio.run(jobs.refresh_handler(None))
    assert result is None
    # 5 sites, batch size 2 -> batches of [2, 2, 1]; no batch is the whole grid.
    assert fetched_batches == [["s0", "s1"], ["s2", "s3"], ["s4"]]
    # Every site is persisted exactly once across the batches.
    assert sorted(sum(persisted, [])) == ["s0", "s1", "s2", "s3", "s4"]


def test_refresh_handler_skips_empty_batches_but_persists_others(monkeypatch):
    # A batch whose fetch returned nothing (all failed) is skipped, but later
    # non-empty batches still persist: stale beats empty, per batch.
    sites = [
        {"id": f"s{i}", "lat": 57.0, "lon": -4.0, "altitude_m": 100} for i in range(4)
    ]
    monkeypatch.setattr(jobs, "_load_sites", lambda: sites)
    monkeypatch.setattr(jobs, "REFRESH_BATCH_SIZE", 2)

    async def _fake_fetch(batch):
        # First batch fetches nothing; second batch succeeds.
        if batch[0]["id"] == "s0":
            return {}
        return {s["id"]: [_hour("2026-06-13T23:00:00Z")] for s in batch}

    persisted = []

    def _fake_persist(scored):
        persisted.append(sorted(scored))
        return len(scored)

    monkeypatch.setattr(jobs, "fetch_all", _fake_fetch)
    monkeypatch.setattr(jobs, "_persist_sites", _fake_persist)

    result = asyncio.run(jobs.refresh_handler(None))
    assert result is None
    # Only the second batch persisted; the empty first batch was skipped.
    assert persisted == [["s2", "s3"]]
