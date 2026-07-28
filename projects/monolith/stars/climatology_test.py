"""Unit tests for the stars ERA5 climatology ingest (ADR 009).

Mirrors stars/grid_test: SQLModel.metadata.create_all on SQLite (no migrations),
_fetch_climatology monkeypatched so the S3 layer is never exercised, and
_load_climatology_sync's get_engine pointed at the in-memory engine so the
wholesale-replace is asserted against a real session.
"""

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import stars.grid as grid
from stars.models import SiteMonthClimatology


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
        # _load_climatology_sync opens its own Session(get_engine()); point it here.
        import core.db as db

        monkeypatch.setattr(db, "get_engine", lambda: engine)
        yield engine
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


_CLIMO = [
    {
        "site_id": "scotland-0001",
        "month": 3,
        "dark_hours": 300,
        "clear_dark_hours": 85,
    },
    {
        "site_id": "scotland-0002",
        "month": 3,
        "dark_hours": 120,
        "clear_dark_hours": 30,
    },
]


def test_load_climatology_sync_upserts_rows(engine, monkeypatch):
    monkeypatch.setattr(grid, "_fetch_climatology", lambda: [dict(r) for r in _CLIMO])

    written = grid._load_climatology_sync()
    assert written == 2

    with Session(engine) as session:
        rows = session.exec(select(SiteMonthClimatology)).all()
        by_id = {r.site_id: r for r in rows}
    assert set(by_id) == {"scotland-0001", "scotland-0002"}
    assert by_id["scotland-0001"].month == 3
    assert by_id["scotland-0001"].dark_hours == 300
    assert by_id["scotland-0001"].clear_dark_hours == 85


def test_load_climatology_sync_replaces_existing_rows(engine, monkeypatch):
    # A stale row not present in the new backfill must be removed (wholesale replace).
    with Session(engine) as session:
        session.add(
            SiteMonthClimatology(
                site_id="stale", month=9, dark_hours=5, clear_dark_hours=2
            )
        )
        session.commit()

    monkeypatch.setattr(grid, "_fetch_climatology", lambda: [dict(_CLIMO[0])])
    written = grid._load_climatology_sync()
    assert written == 1

    with Session(engine) as session:
        keys = {
            (r.site_id, r.month)
            for r in session.exec(select(SiteMonthClimatology)).all()
        }
    assert keys == {("scotland-0001", 3)}


def test_load_climatology_sync_skips_malformed_rows(engine, monkeypatch):
    payload = [
        {"site_id": "good", "month": 6, "dark_hours": 30, "clear_dark_hours": 5},
        {"site_id": "no-month"},  # missing month -> skipped
        {"month": 6},  # missing site_id -> skipped
        {"site_id": "bad-month", "month": 13},  # month out of range -> skipped
        {"site_id": "non-numeric", "month": "abc"},  # unparseable month -> skipped
        "not-a-dict",  # skipped
    ]
    monkeypatch.setattr(grid, "_fetch_climatology", lambda: payload)

    written = grid._load_climatology_sync()
    assert written == 1
    with Session(engine) as session:
        ids = {r.site_id for r in session.exec(select(SiteMonthClimatology)).all()}
    assert ids == {"good"}


def test_load_climatology_sync_empty_is_noop(engine, monkeypatch):
    monkeypatch.setattr(grid, "_fetch_climatology", lambda: None)
    assert grid._load_climatology_sync() == 0


def test_fetch_climatology_unset_endpoint_returns_none(monkeypatch):
    monkeypatch.delenv("SEAWEEDFS_S3_ENDPOINT", raising=False)
    assert grid._fetch_climatology() is None

    monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "   ")
    assert grid._fetch_climatology() is None
