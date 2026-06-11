"""Unit tests for ships/router.py: /api/ships/snapshot and /track/{mmsi}.

Uses an in-memory SQLite DB seeded with real rows and a minimal FastAPI app
that mounts only the ships router, mirroring the schema-stripping +
``app.dependency_overrides[get_session]`` pattern in
``knowledge/note_review_endpoints_test.py`` and ``ships/store_test.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.db import get_session
from ships.models import LatestPosition, Position, Vessel
from ships.router import _parse_since, router

T0 = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # SQLite can't span schemas, so strip the Postgres-only schema= overrides so
    # SQLModel.metadata.create_all() lands every table in the default schema.
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


@pytest.fixture(name="client")
def client_fixture(session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed_snapshot(session: Session) -> None:
    session.add(
        Vessel(mmsi="316001234", name="ALPHA", ship_type=70, destination="VANCOUVER")
    )
    session.add(Vessel(mmsi="316005678", name="BRAVO", ship_type=80))
    session.add(
        LatestPosition(
            mmsi="316001234",
            lat=49.28,
            lon=-123.12,
            speed=12.5,
            course=180.0,
            heading=182,
            nav_status=0,
            ship_name="ALPHA POS",
            recorded_at=T0,
            first_seen_at_location=T0 - timedelta(hours=2),
            updated_at=T0,
        )
    )
    session.add(
        LatestPosition(
            mmsi="316005678",
            lat=48.5,
            lon=-122.0,
            speed=0.0,
            recorded_at=T0 + timedelta(minutes=5),
            updated_at=T0 + timedelta(minutes=5),
        )
    )
    session.commit()


class TestSnapshot:
    def test_returns_merged_vessels(self, client, session):
        _seed_snapshot(session)
        r = client.get("/api/ships/snapshot")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 2
        assert len(body["vessels"]) == 2

        by_mmsi = {v["mmsi"]: v for v in body["vessels"]}
        alpha = by_mmsi["316001234"]
        # Position-message ship_name wins over the vessel name.
        assert alpha["ship_name"] == "ALPHA POS"
        assert alpha["name"] == "ALPHA"
        assert alpha["ship_type"] == 70
        assert alpha["destination"] == "VANCOUVER"
        assert alpha["lat"] == 49.28
        assert alpha["speed"] == 12.5
        assert alpha["first_seen_at_location"] is not None

        # No position ship_name falls back to the vessel name.
        bravo = by_mmsi["316005678"]
        assert bravo["ship_name"] == "BRAVO"

    def test_cache_and_etag_headers(self, client, session):
        _seed_snapshot(session)
        r = client.get("/api/ships/snapshot")
        assert r.status_code == 200
        assert (
            r.headers["Cache-Control"]
            == "public, s-maxage=120, stale-while-revalidate=600, stale-if-error=86400"
        )
        assert r.headers["ETag"]

    def test_conditional_get_returns_304(self, client, session):
        _seed_snapshot(session)
        first = client.get("/api/ships/snapshot")
        etag = first.headers["ETag"]
        second = client.get("/api/ships/snapshot", headers={"If-None-Match": etag})
        assert second.status_code == 304
        assert second.headers["ETag"] == etag
        assert second.headers["Cache-Control"]

    def test_empty_snapshot(self, client):
        r = client.get("/api/ships/snapshot")
        assert r.status_code == 200
        assert r.json() == {"count": 0, "vessels": []}


def _seed_track(session: Session) -> None:
    # Seed relative to now: the /track since= filter is computed from
    # datetime.now(), so fixed past timestamps would fall outside any recent
    # window once the calendar advances. base, base-1h, base-2h lets since=90m
    # select exactly the first two.
    base = datetime.now(timezone.utc)
    for i in range(3):
        session.add(
            Position(
                mmsi="316001234",
                lat=49.0 + i,
                lon=-123.0 - i,
                speed=float(i),
                course=10.0 * i,
                heading=i,
                nav_status=0,
                recorded_at=base - timedelta(hours=i),
            )
        )
    session.commit()


class TestTrack:
    def test_newest_first(self, client, session):
        _seed_track(session)
        r = client.get("/api/ships/track/316001234")
        assert r.status_code == 200
        body = r.json()
        assert body["mmsi"] == "316001234"
        assert body["count"] == 3
        recorded = [p["recorded_at"] for p in body["track"]]
        assert recorded == sorted(recorded, reverse=True)

    def test_since_filters_older(self, client, session):
        _seed_track(session)
        # T0 and T0-1h fall within 90 minutes; T0-2h does not.
        r = client.get("/api/ships/track/316001234?since=90m")
        assert r.status_code == 200
        assert r.json()["count"] == 2

    def test_limit_caps(self, client, session):
        _seed_track(session)
        r = client.get("/api/ships/track/316001234?limit=1")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1

    def test_unknown_mmsi_returns_empty(self, client):
        r = client.get("/api/ships/track/000000000")
        assert r.status_code == 200
        assert r.json() == {"mmsi": "000000000", "count": 0, "track": []}

    def test_cache_header(self, client, session):
        _seed_track(session)
        r = client.get("/api/ships/track/316001234")
        assert (
            r.headers["Cache-Control"]
            == "public, s-maxage=60, stale-while-revalidate=300, stale-if-error=86400"
        )


class TestParseSince:
    def test_hours(self):
        assert _parse_since("1h") == timedelta(hours=1)

    def test_minutes(self):
        assert _parse_since("30m") == timedelta(minutes=30)

    def test_days(self):
        assert _parse_since("2d") == timedelta(days=2)

    def test_invalid_returns_none(self):
        assert _parse_since("bogus") is None
        assert _parse_since("h") is None
        assert _parse_since("") is None
        assert _parse_since(None) is None
