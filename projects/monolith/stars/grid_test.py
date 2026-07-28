"""Unit tests for stars.grid: the SeaweedFS grid -> stars.sites ingest.

Uses SQLModel.metadata.create_all on SQLite (no migrations), mirroring
stars/models_test. _fetch_grid is monkeypatched so the S3 layer is never
exercised; _load_grid_sync's get_engine is pointed at the in-memory engine so
the upsert is asserted against a real session.
"""

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from datetime import datetime, timezone

import stars.grid as grid
from stars.models import Site, SiteHour


@pytest.fixture(name="engine")
def engine_fixture(monkeypatch):
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
        # _load_grid_sync opens its own Session(get_engine()); point it here.
        import core.db as db

        monkeypatch.setattr(db, "get_engine", lambda: engine)
        yield engine
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


_GRID = [
    {
        "id": "grid-0001",
        "lat": 57.12,
        "lon": -4.70,
        "altitude_m": 312,
        "lp_zone": "1a",
        "name": None,
    },
    {
        "id": "grid-0002",
        "lat": 58.0,
        "lon": -5.0,
        "altitude_m": 100,
        "lp_zone": "2b",
        "name": "Named",
    },
]


def test_load_grid_sync_upserts_rows(engine, monkeypatch):
    monkeypatch.setattr(grid, "_fetch_grid", lambda: list(_GRID))

    written = grid._load_grid_sync()
    assert written == 2

    with Session(engine) as session:
        rows = session.exec(select(Site)).all()
        by_id = {r.id: r for r in rows}
    assert set(by_id) == {"grid-0001", "grid-0002"}
    assert by_id["grid-0001"].name is None
    assert by_id["grid-0001"].lat == 57.12
    assert by_id["grid-0001"].altitude_m == 312
    assert by_id["grid-0001"].lp_zone == "1a"
    assert by_id["grid-0001"].source == "grid"
    assert by_id["grid-0002"].name == "Named"


def test_load_grid_sync_replaces_existing_rows(engine, monkeypatch):
    # A stale site not present in the new grid must be removed (wholesale replace).
    with Session(engine) as session:
        session.add(Site(id="stale", lat=1.0, lon=2.0, source="grid"))
        session.commit()

    monkeypatch.setattr(grid, "_fetch_grid", lambda: [_GRID[0]])
    written = grid._load_grid_sync()
    assert written == 1

    with Session(engine) as session:
        ids = {r.id for r in session.exec(select(Site)).all()}
    assert ids == {"grid-0001"}


def test_load_grid_sync_skips_malformed_points(engine, monkeypatch):
    payload = [
        {"id": "good", "lat": 57.0, "lon": -4.0},
        {"id": "no-coords"},  # missing lat/lon -> skipped
        {"lat": 1.0, "lon": 2.0},  # missing id -> skipped
        "not-a-dict",  # skipped
    ]
    monkeypatch.setattr(grid, "_fetch_grid", lambda: payload)

    written = grid._load_grid_sync()
    assert written == 1
    with Session(engine) as session:
        ids = {r.id for r in session.exec(select(Site)).all()}
    assert ids == {"good"}


def test_load_grid_sync_removes_orphaned_site_hours(engine, monkeypatch):
    # A site that drops out of the grid must have its forecast hours cleaned;
    # hours for sites still in the grid survive (ADR 008 orphan clean).
    hour = datetime(2026, 6, 13, 22, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add(Site(id="stale", lat=1.0, lon=2.0, source="grid"))
        session.add(
            SiteHour(
                site_id="stale",
                hour_time=hour,
                cloud_area_fraction=10.0,
                air_temperature=8.0,
                dew_spread=5.0,
                sun_elevation_deg=-18.0,
            )
        )
        session.add(
            SiteHour(
                site_id="grid-0001",
                hour_time=hour,
                cloud_area_fraction=10.0,
                air_temperature=8.0,
                dew_spread=5.0,
                sun_elevation_deg=-18.0,
            )
        )
        session.commit()

    monkeypatch.setattr(grid, "_fetch_grid", lambda: [_GRID[0]])
    written = grid._load_grid_sync()
    assert written == 1

    with Session(engine) as session:
        hour_site_ids = {r.site_id for r in session.exec(select(SiteHour)).all()}
    # The orphaned site's hour is gone; the surviving grid site's hour remains.
    assert hour_site_ids == {"grid-0001"}


def test_load_grid_sync_empty_grid_is_noop(engine, monkeypatch):
    monkeypatch.setattr(grid, "_fetch_grid", lambda: None)
    assert grid._load_grid_sync() == 0


def test_fetch_grid_unset_endpoint_returns_none(monkeypatch):
    monkeypatch.delenv("SEAWEEDFS_S3_ENDPOINT", raising=False)
    assert grid._fetch_grid() is None

    monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "   ")
    assert grid._fetch_grid() is None
