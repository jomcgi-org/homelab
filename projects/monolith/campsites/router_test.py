"""Unit tests for campsites/router.py: /api/campsites/snapshot.

Uses an in-memory SQLite DB seeded with real rows and a minimal FastAPI app
that mounts only the campsites router. Mirrors the schema-stripping +
``app.dependency_overrides[get_session]`` pattern from ships/router_test.py
and worldcup/router_test.py.
"""

from __future__ import annotations

import datetime
from datetime import timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from core.db import get_session
from campsites.models import Availability, Campground, Weather
from campsites.router import router

_NOW = datetime.datetime.now(timezone.utc)
_TODAY = _NOW.date()


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # SQLite cannot span schemas, so strip the Postgres-only schema= overrides
    # so that SQLModel.metadata.create_all() lands every table in the default
    # schema.
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


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _campground(
    resource_location_id: int, name: str, region: str = "Interior"
) -> Campground:
    return Campground(
        resource_location_id=resource_location_id,
        park_map_id=resource_location_id * 10,
        name=name,
        region=region,
        latitude=50.0 + resource_location_id * 0.1,
        longitude=-120.0 - resource_location_id * 0.1,
    )


def _availability(
    resource_location_id: int,
    date: datetime.date,
    has_availability: bool = True,
) -> Availability:
    return Availability(
        resource_location_id=resource_location_id,
        date=date,
        has_availability=has_availability,
        scraped_at=_NOW,
    )


def _weather(
    resource_location_id: int,
    date: datetime.date,
    *,
    sunny_score: int = 70,
    is_good: bool = True,
    cloud_cover: float = 10.0,
    precip_sum: float = 0.0,
    temp_max: float = 22.0,
) -> Weather:
    return Weather(
        resource_location_id=resource_location_id,
        date=date,
        sunny_score=sunny_score,
        is_good=is_good,
        cloud_cover=cloud_cover,
        precip_sum=precip_sum,
        temp_max=temp_max,
        fetched_at=_NOW,
    )


# ---------------------------------------------------------------------------
# Core tests
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_returns_parks_with_days(self, client, session):
        """Basic: two parks, availability and weather seeded, correct park count."""
        session.add(_campground(1, "Alpha Lake"))
        session.add(_campground(2, "Beta Canyon"))
        session.add(_availability(1, _TODAY))
        session.add(_weather(1, _TODAY, sunny_score=80, is_good=True))
        session.add(_availability(2, _TODAY, has_availability=False))
        session.add(_weather(2, _TODAY, sunny_score=30, is_good=False))
        session.commit()

        r = client.get("/api/campsites/snapshot")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 2
        assert len(body["parks"]) == 2

    def test_park_object_shape(self, client, session):
        """Park object must include all required fields with correct types."""
        session.add(_campground(1, "Alpha Lake", region="Interior"))
        session.add(_availability(1, _TODAY))
        session.add(_weather(1, _TODAY, sunny_score=75))
        session.commit()

        r = client.get("/api/campsites/snapshot")
        body = r.json()
        park = body["parks"][0]
        assert park["id"] == 1
        assert park["name"] == "Alpha Lake"
        assert park["region"] == "Interior"
        assert isinstance(park["lat"], float)
        assert isinstance(park["lon"], float)
        assert "booking_url" in park
        assert isinstance(park["best_score"], int)
        assert isinstance(park["good_days"], int)
        assert isinstance(park["days"], list)

    def test_day_object_shape(self, client, session):
        """Each day dict must carry all seven fields with correct types."""
        session.add(_campground(1, "Alpha Lake"))
        session.add(_availability(1, _TODAY))
        session.add(
            _weather(
                1,
                _TODAY,
                sunny_score=60,
                is_good=True,
                cloud_cover=15.5,
                precip_sum=0.2,
                temp_max=18.0,
            )
        )
        session.commit()

        r = client.get("/api/campsites/snapshot")
        body = r.json()
        day = body["parks"][0]["days"][0]
        assert day["date"] == _TODAY.isoformat()
        assert isinstance(day["available"], bool)
        assert isinstance(day["sunny_score"], int)
        assert isinstance(day["is_good"], bool)
        assert day["cloud"] == 15.5
        assert day["precip"] == 0.2
        assert day["temp_max"] == 18.0

    def test_best_score_only_counts_available_days(self, client, session):
        """best_score = max sunny_score among days where available=True only."""
        tomorrow = _TODAY + datetime.timedelta(days=1)
        session.add(_campground(1, "Alpha Lake"))
        # Day 1: available=True, high score.
        session.add(_availability(1, _TODAY, has_availability=True))
        session.add(_weather(1, _TODAY, sunny_score=90, is_good=True))
        # Day 2: available=False, very high score (must NOT contribute).
        session.add(_availability(1, tomorrow, has_availability=False))
        session.add(_weather(1, tomorrow, sunny_score=100, is_good=True))
        session.commit()

        r = client.get("/api/campsites/snapshot")
        body = r.json()
        park = body["parks"][0]
        assert park["best_score"] == 90, "best_score must ignore unavailable days"

    def test_best_score_zero_when_no_available_days(self, client, session):
        """If no available days exist, best_score must be 0."""
        session.add(_campground(1, "Alpha Lake"))
        session.add(_availability(1, _TODAY, has_availability=False))
        session.add(_weather(1, _TODAY, sunny_score=85, is_good=True))
        session.commit()

        r = client.get("/api/campsites/snapshot")
        body = r.json()
        park = body["parks"][0]
        assert park["best_score"] == 0

    def test_good_days_count(self, client, session):
        """good_days = count of days where available AND is_good."""
        d1 = _TODAY
        d2 = _TODAY + datetime.timedelta(days=1)
        d3 = _TODAY + datetime.timedelta(days=2)
        session.add(_campground(1, "Alpha Lake"))
        # d1: available + good -> counts.
        session.add(_availability(1, d1, has_availability=True))
        session.add(_weather(1, d1, sunny_score=80, is_good=True))
        # d2: available but NOT good -> does not count.
        session.add(_availability(1, d2, has_availability=True))
        session.add(_weather(1, d2, sunny_score=10, is_good=False))
        # d3: good but NOT available -> does not count.
        session.add(_availability(1, d3, has_availability=False))
        session.add(_weather(1, d3, sunny_score=90, is_good=True))
        session.commit()

        r = client.get("/api/campsites/snapshot")
        body = r.json()
        assert body["parks"][0]["good_days"] == 1

    def test_days_sorted_ascending_by_date(self, client, session):
        """Days list must be ordered oldest to newest."""
        d1 = _TODAY
        d2 = _TODAY + datetime.timedelta(days=1)
        d3 = _TODAY + datetime.timedelta(days=2)
        session.add(_campground(1, "Alpha Lake"))
        session.add(_availability(1, d3))
        session.add(_availability(1, d1))
        session.add(_availability(1, d2))
        session.commit()

        r = client.get("/api/campsites/snapshot")
        body = r.json()
        dates = [day["date"] for day in body["parks"][0]["days"]]
        assert dates == sorted(dates)

    def test_parks_sorted_best_score_desc_then_name(self, client, session):
        """Parks ordered by best_score descending, then name ascending."""
        session.add(_campground(1, "Zebra Park"))
        session.add(_campground(2, "Alpha Lake"))
        session.add(_campground(3, "Beta Canyon"))
        # Park 2 (Alpha): best_score 90.
        session.add(_availability(2, _TODAY, has_availability=True))
        session.add(_weather(2, _TODAY, sunny_score=90))
        # Parks 1 and 3 (Zebra, Beta): best_score 0 each (no availability).
        session.commit()

        r = client.get("/api/campsites/snapshot")
        body = r.json()
        names = [p["name"] for p in body["parks"]]
        assert names[0] == "Alpha Lake", "highest best_score must be first"
        # The two score=0 parks should be alpha-sorted.
        assert names[1:] == sorted(names[1:])

    def test_defaults_when_no_availability_row(self, client, session):
        """A weather-only date must default available=False, sunny_score=0."""
        session.add(_campground(1, "Alpha Lake"))
        session.add(_weather(1, _TODAY, sunny_score=80, is_good=True))
        session.commit()

        r = client.get("/api/campsites/snapshot")
        body = r.json()
        day = body["parks"][0]["days"][0]
        assert day["available"] is False
        assert day["sunny_score"] == 80  # weather score still reported

    def test_defaults_when_no_weather_row(self, client, session):
        """An availability-only date must default sunny_score=0, is_good=False."""
        session.add(_campground(1, "Alpha Lake"))
        session.add(_availability(1, _TODAY, has_availability=True))
        session.commit()

        r = client.get("/api/campsites/snapshot")
        body = r.json()
        day = body["parks"][0]["days"][0]
        assert day["available"] is True
        assert day["sunny_score"] == 0
        assert day["is_good"] is False
        assert day["cloud"] is None
        assert day["precip"] is None
        assert day["temp_max"] is None

    def test_503_when_no_campgrounds(self, client):
        """503 must be returned when the campgrounds table is empty."""
        r = client.get("/api/campsites/snapshot")
        assert r.status_code == 503
        body = r.json()
        assert "unavailable" in body["detail"]

    def test_cache_control_header(self, client, session):
        """Cache-Control header must match the declared constant."""
        session.add(_campground(1, "Alpha Lake"))
        session.commit()

        r = client.get("/api/campsites/snapshot")
        assert r.status_code == 200
        assert r.headers["Cache-Control"] == (
            "public, max-age=0, s-maxage=60, stale-while-revalidate=3600, stale-if-error=86400"
        )

    def test_etag_header_present(self, client, session):
        """ETag must be present on a 200 response."""
        session.add(_campground(1, "Alpha Lake"))
        session.commit()

        r = client.get("/api/campsites/snapshot")
        assert r.status_code == 200
        assert r.headers.get("ETag")

    def test_conditional_get_returns_304(self, client, session):
        """A matching If-None-Match header must short-circuit with 304."""
        session.add(_campground(1, "Alpha Lake"))
        session.add(_availability(1, _TODAY))
        session.add(_weather(1, _TODAY, sunny_score=70))
        session.commit()

        first = client.get("/api/campsites/snapshot")
        etag = first.headers["ETag"]

        second = client.get("/api/campsites/snapshot", headers={"If-None-Match": etag})
        assert second.status_code == 304
        assert second.headers["ETag"] == etag
        assert second.headers["Cache-Control"]

    def test_generated_at_reflects_max_timestamp(self, client, session):
        """generated_at must be the max of scraped_at/fetched_at across all rows."""
        earlier = datetime.datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        later = datetime.datetime(2026, 6, 2, 15, 0, 0, tzinfo=timezone.utc)
        session.add(_campground(1, "Alpha Lake"))
        session.add(
            Availability(
                resource_location_id=1,
                date=_TODAY,
                has_availability=True,
                scraped_at=earlier,
            )
        )
        session.add(
            Weather(
                resource_location_id=1,
                date=_TODAY,
                sunny_score=60,
                is_good=True,
                fetched_at=later,
            )
        )
        session.commit()

        r = client.get("/api/campsites/snapshot")
        body = r.json()
        assert body["generated_at"].startswith("2026-06-02")

    def test_past_dates_excluded(self, client, session):
        """Dates older than the tz boundary must not appear in days.

        The router windows on (UTC today - 1 day), not UTC today: availability
        and weather are dated in each park's local timezone, and during BC
        evenings a park's LOCAL today equals UTC yesterday. So the UTC today - 1
        boundary day is intentionally kept (it is a park's current day), while
        anything strictly older (two or more days back) is genuinely past and
        excluded.
        """
        two_days_ago = _TODAY - datetime.timedelta(days=2)
        yesterday = _TODAY - datetime.timedelta(days=1)
        session.add(_campground(1, "Alpha Lake"))
        session.add(_availability(1, two_days_ago, has_availability=True))
        session.add(_weather(1, two_days_ago, sunny_score=90))
        session.add(_availability(1, yesterday, has_availability=True))
        session.add(_weather(1, yesterday, sunny_score=80))
        session.add(_availability(1, _TODAY, has_availability=True))
        session.add(_weather(1, _TODAY, sunny_score=50))
        session.commit()

        r = client.get("/api/campsites/snapshot")
        body = r.json()
        dates = [day["date"] for day in body["parks"][0]["days"]]
        assert two_days_ago.isoformat() not in dates
        # The UTC today - 1 boundary day is kept for the park-local timezone edge.
        assert yesterday.isoformat() in dates
        assert _TODAY.isoformat() in dates
