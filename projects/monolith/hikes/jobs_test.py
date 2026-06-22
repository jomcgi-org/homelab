"""Unit tests for hikes.jobs DB cores on SQLite.

Uses SQLModel.metadata.create_all (no migrations), mirroring stars/jobs_test.
The async handlers delegate their DB work to synchronous cores
(_write_walk_hours, _prune_elapsed) via asyncio.to_thread with a fresh engine
session; the cores take an explicit session so they are testable against the
in-memory fixture. The thin async wrappers are not unit tested here.

Window tuples are [ts_unix_seconds, temp_c, precip_mm, wind_kmh, cloud_pct],
exactly what hikes.forecast.compute_windows emits and what _write_walk_hours
maps into typed WalkHour rows.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import hikes.jobs as jobs
from hikes.models import Walk, WalkHour
from shared.forecast_freshness import top_of_hour


def _scraped(uuid, **over):
    """A scraped Walk (hikes.walkhighlands.Walk == hikes.models.Walk)."""
    fields = dict(
        uuid=uuid,
        name="A Walk",
        url=f"https://example.invalid/{uuid}.shtml",
        distance_km=10.0,
        ascent_m=500,
        duration_h=4.0,
        summary="A fine walk.",
        latitude=57.0,
        longitude=-4.0,
    )
    fields.update(over)
    return Walk(**fields)


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


def _ts(dt: datetime) -> int:
    return int(dt.timestamp())


def _seed_hour(session, walk_uuid, hour_time, temp_c=10.0):
    session.add(
        WalkHour(
            walk_uuid=walk_uuid,
            hour_time=hour_time,
            temp_c=temp_c,
            precip_mm=0.0,
            wind_kmh=12.0,
            cloud_pct=40.0,
        )
    )


def test_write_walk_hours_inserts_rows_for_each_walk(session):
    h1 = datetime(2026, 6, 13, 23, tzinfo=timezone.utc)
    h2 = datetime(2026, 6, 13, 22, tzinfo=timezone.utc)
    windows_by_uuid = {
        "walk-a": [[_ts(h1), 12.5, 0, 14, 40]],
        "walk-b": [[_ts(h2), 11.0, 0.5, 18, 55], [_ts(h1), 10.0, 0, 20, 60]],
    }
    now = datetime(2026, 6, 13, 21, tzinfo=timezone.utc)

    written = jobs._write_walk_hours(session, windows_by_uuid, now)
    session.commit()
    assert written == 3

    rows = session.exec(select(WalkHour)).all()
    assert len(rows) == 3

    a = session.get(WalkHour, ("walk-a", h1))
    assert a is not None
    assert a.temp_c == 12.5
    assert a.wind_kmh == 14
    # A single shared fetched_at is stamped across the whole run.
    assert len({r.fetched_at for r in rows}) == 1


def test_write_walk_hours_leaves_unfetched_walks_untouched(session):
    # Stale beats empty: the bulk delete only targets fetched walk uuids, so a
    # walk absent from the windows map keeps its previous rows.
    kept = datetime(2026, 6, 13, 21, tzinfo=timezone.utc)
    _seed_hour(session, "X", kept, temp_c=8.0)
    session.commit()

    now = datetime(2026, 6, 13, 22, tzinfo=timezone.utc)
    new = datetime(2026, 6, 13, 23, tzinfo=timezone.utc)
    jobs._write_walk_hours(session, {"A": [[_ts(new), 9.0, 0, 10, 30]]}, now)
    session.commit()

    survivor = session.get(WalkHour, ("X", kept))
    assert survivor is not None
    assert survivor.temp_c == 8.0
    assert session.get(WalkHour, ("A", new)) is not None


def test_write_walk_hours_replaces_walk_rows_wholesale(session):
    old_a = datetime(2026, 6, 13, 20, tzinfo=timezone.utc)
    old_b = datetime(2026, 6, 13, 21, tzinfo=timezone.utc)
    _seed_hour(session, "A", old_a, temp_c=1.0)
    _seed_hour(session, "A", old_b, temp_c=2.0)
    session.commit()

    now = datetime(2026, 6, 13, 22, tzinfo=timezone.utc)
    new_a = datetime(2026, 6, 13, 23, tzinfo=timezone.utc)
    new_b = datetime(2026, 6, 14, 0, tzinfo=timezone.utc)
    windows_by_uuid = {
        "A": [[_ts(new_a), 12.5, 0, 14, 40], [_ts(new_b), 11.0, 0, 16, 50]]
    }
    jobs._write_walk_hours(session, windows_by_uuid, now)
    session.commit()

    rows = session.exec(select(WalkHour).where(WalkHour.walk_uuid == "A")).all()
    assert {_utc(r.hour_time) for r in rows} == {new_a, new_b}


def test_write_walk_hours_empty_list_clears_a_fetched_walk(session):
    # A walk present with an empty window list (fetch succeeded, nothing viable)
    # has its stale rows removed and gains none.
    old = datetime(2026, 6, 13, 20, tzinfo=timezone.utc)
    _seed_hour(session, "A", old)
    session.commit()

    now = datetime(2026, 6, 13, 22, tzinfo=timezone.utc)
    written = jobs._write_walk_hours(session, {"A": []}, now)
    session.commit()
    assert written == 0
    assert session.exec(select(WalkHour).where(WalkHour.walk_uuid == "A")).all() == []


def test_prune_elapsed_drops_only_elapsed_hours(session):
    cutoff = top_of_hour(datetime.now(timezone.utc))
    past_a = cutoff - timedelta(hours=3)
    past_b = cutoff - timedelta(hours=1)
    current = cutoff
    future = cutoff + timedelta(hours=2)
    for i, h in enumerate((past_a, past_b, current, future)):
        _seed_hour(session, f"walk-{i}", h)
    session.commit()

    deleted = jobs._prune_elapsed(session)
    session.commit()
    assert deleted == 2

    remaining = {_utc(r.hour_time) for r in session.exec(select(WalkHour)).all()}
    assert remaining == {current, future}


def test_upsert_walks_inserts_new(session):
    new, updated = jobs._upsert_walks(session, [_scraped("u1"), _scraped("u2")])
    session.commit()
    assert (new, updated) == (2, 0)
    assert {w.uuid for w in session.exec(select(Walk)).all()} == {"u1", "u2"}


def test_upsert_walks_updates_existing(session):
    session.add(_scraped("u1", duration_h=66.0, name="Old"))
    session.commit()
    new, updated = jobs._upsert_walks(session, [_scraped("u1", duration_h=18.0)])
    session.commit()
    assert (new, updated) == (0, 1)
    row = session.get(Walk, "u1")
    assert row.duration_h == 18.0 and row.name == "A Walk"


def test_upsert_walks_dedupes_batch_by_uuid(session):
    # Two walk pages at the same trailhead share uuid5(lat,lon). Both would take
    # the insert path and trip walks_pkey; dedup keeps the last and the commit
    # must succeed (the bug that froze the corpus once scraping worked again).
    new, updated = jobs._upsert_walks(
        session,
        [_scraped("dup", duration_h=18.0), _scraped("dup", duration_h=12.0)],
    )
    session.commit()  # must not raise UniqueViolation
    assert (new, updated) == (1, 0)
    rows = session.exec(select(Walk).where(Walk.uuid == "dup")).all()
    assert len(rows) == 1
    assert rows[0].duration_h == 12.0  # last occurrence wins
