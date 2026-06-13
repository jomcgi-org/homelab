"""Unit tests for hikes/router.py: /api/hikes/walks.

Uses an in-memory SQLite DB seeded with real rows and a minimal FastAPI app
that mounts only the hikes router, mirroring the schema-stripping +
``app.dependency_overrides[get_session]`` pattern in ``ships/router_test.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.db import get_session
from hikes.models import Walk
from hikes.router import router

T0 = datetime(2026, 6, 12, 6, 0, 0, tzinfo=timezone.utc)

WINDOWS = [
    ["2026-06-13T09:00:00+00:00", 14.2, 0.0, 12.5, 40],
    ["2026-06-13T10:00:00+00:00", 15.1, 0.3, 18.0, 55],
]


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


def _seed_walks(session: Session) -> None:
    session.add(
        Walk(
            uuid="aaaaaaaa-0000-5000-8000-000000000001",
            name="Ben Vorlich from Ardlui",
            url="https://www.walkhighlands.co.uk/lochlomond/ben-vorlich-ardlui.shtml",
            distance_km=11.0,
            ascent_m=920,
            duration_h=5.5,
            summary="A steep but rewarding Munro above Loch Lomond.",
            latitude=56.2734,
            longitude=-4.7521,
            windows=WINDOWS,
            windows_updated_at=T0,
        )
    )
    session.add(
        Walk(
            uuid="aaaaaaaa-0000-5000-8000-000000000002",
            name="An Teallach circuit",
            url="https://www.walkhighlands.co.uk/torridon/an-teallach.shtml",
            distance_km=18.5,
            ascent_m=1450,
            duration_h=10.0,
            summary="",
            latitude=57.8067,
            longitude=-5.2596,
        )
    )
    session.commit()


class TestWalks:
    def test_payload_shape_and_ordering(self, client, session):
        _seed_walks(session)
        r = client.get("/api/hikes/walks")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 2
        assert body["generated_at"] == T0.isoformat()
        assert len(body["walks"]) == 2

        # Ordered by name: "An Teallach circuit" before "Ben Vorlich from Ardlui".
        names = [w["name"] for w in body["walks"]]
        assert names == ["An Teallach circuit", "Ben Vorlich from Ardlui"]

        ben = body["walks"][1]
        assert ben["uuid"] == "aaaaaaaa-0000-5000-8000-000000000001"
        assert ben["url"].endswith("ben-vorlich-ardlui.shtml")
        assert ben["distance_km"] == 11.0
        assert ben["ascent_m"] == 920
        assert ben["duration_h"] == 5.5
        assert ben["summary"] == "A steep but rewarding Munro above Loch Lomond."
        assert ben["latitude"] == 56.2734
        assert ben["longitude"] == -4.7521
        # The windows list comes through intact.
        assert ben["windows"] == WINDOWS

        # The walk without a forecast has empty windows.
        an_teallach = body["walks"][0]
        assert an_teallach["windows"] == []

    def test_cache_and_etag_headers(self, client, session):
        _seed_walks(session)
        r = client.get("/api/hikes/walks")
        assert r.status_code == 200
        assert (
            r.headers["Cache-Control"]
            == "public, s-maxage=1800, stale-while-revalidate=3600, stale-if-error=86400"
        )
        assert r.headers["ETag"]

    def test_conditional_get_returns_304(self, client, session):
        _seed_walks(session)
        first = client.get("/api/hikes/walks")
        etag = first.headers["ETag"]
        second = client.get("/api/hikes/walks", headers={"If-None-Match": etag})
        assert second.status_code == 304
        assert second.headers["ETag"] == etag
        assert second.headers["Cache-Control"]

    def test_empty_corpus(self, client):
        r = client.get("/api/hikes/walks")
        assert r.status_code == 200
        assert r.json() == {"count": 0, "generated_at": None, "walks": []}
