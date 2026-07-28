"""Unit tests for the public trips SSR read router.

FastAPI TestClient over an in-memory SQLite session (SQLModel.create_all, no
migrations), mirroring trips/ingest_router_test.py's schema-stripping fixture.
"""

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from core.db import get_session
from trips.models import Trip, TripPoint
from trips.read_router import router as read_router


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


@pytest.fixture(name="client")
def client_fixture(session):
    app = FastAPI()
    app.include_router(read_router)
    app.dependency_overrides[get_session] = lambda: session
    with TestClient(app) as client:
        yield client


def _seed(session):
    session.add(
        Trip(
            slug="2025-liard-hot-springs",
            title="Liard Hot Springs",
            short_title="Liard",
            subtitle="Up the Alaska Highway",
            default_image="img_a.jpg",
            default_zoom=7,
        )
    )
    # Insert out of chronological order so the order_by(taken_at) is exercised.
    session.add(
        TripPoint(
            trip_slug="2025-liard-hot-springs",
            id="p2",
            lat=59.0,
            lng=-126.1,
            taken_at=datetime(2025, 7, 2, 12, 0, tzinfo=timezone.utc),
            image="img_b.jpg",
        )
    )
    session.add(
        TripPoint(
            trip_slug="2025-liard-hot-springs",
            id="p1",
            lat=58.0,
            lng=-125.0,
            taken_at=datetime(2025, 7, 1, 9, 0, tzinfo=timezone.utc),
            image="img_a.jpg",
        )
    )
    session.commit()


def test_get_trip_returns_metadata_and_ordered_points(client, session):
    _seed(session)
    resp = client.get("/api/trips/trip/2025-liard-hot-springs")
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"]

    body = resp.json()
    assert body["trip"]["slug"] == "2025-liard-hot-springs"
    assert body["trip"]["title"] == "Liard Hot Springs"
    assert body["trip"]["default_zoom"] == 7

    ids = [p["id"] for p in body["points"]]
    assert ids == ["p1", "p2"]  # ordered by taken_at ascending


def test_get_trip_missing_is_404(client, session):
    _seed(session)
    resp = client.get("/api/trips/trip/does-not-exist")
    assert resp.status_code == 404


def test_list_trips(client, session):
    _seed(session)
    resp = client.get("/api/trips/trips")
    assert resp.status_code == 200, resp.text
    assert resp.headers["cache-control"]

    body = resp.json()
    assert body["count"] == 1
    slugs = [t["slug"] for t in body["trips"]]
    assert slugs == ["2025-liard-hot-springs"]
    assert body["trips"][0]["subtitle"] == "Up the Alaska Highway"
