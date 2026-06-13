"""Unit tests for hikes/router.py: /api/hikes/walks (+ /{uuid} detail).

Uses an in-memory SQLite DB seeded with real rows and a minimal FastAPI app
that mounts only the hikes router, mirroring the schema-stripping +
``app.dependency_overrides[get_session]`` pattern in ``stars/router_test.py``.

Hours are anchored on ``top_of_hour(now)`` so the read-time cutoff keeps the
seeded "future" rows and drops the seeded "elapsed" one, the same way
stars/router_test pins its timestamps to the cutoff.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.db import get_session
from hikes.models import Walk, WalkHour
from hikes.router import router
from shared.forecast_freshness import top_of_hour

CUTOFF = top_of_hour(datetime.now(timezone.utc))
H1 = CUTOFF + timedelta(hours=1)
H2 = CUTOFF + timedelta(hours=2)
ELAPSED = CUTOFF - timedelta(hours=1)
FETCHED = datetime(2026, 6, 12, 6, 0, 0, tzinfo=timezone.utc)

BEN = "aaaaaaaa-0000-5000-8000-000000000001"
TEALLACH = "aaaaaaaa-0000-5000-8000-000000000002"


def _uk_days(*hours: datetime) -> list[str]:
    """Expected viable_days for a set of hour_times, mirroring router._viable_days."""
    uk = ZoneInfo("Europe/London")
    return sorted({h.astimezone(uk).date().isoformat() for h in hours})


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


def _hour(
    walk_uuid, hour_time, temp_c=12.5, precip_mm=0.0, wind_kmh=14.0, cloud_pct=40.0
):
    return WalkHour(
        walk_uuid=walk_uuid,
        hour_time=hour_time,
        temp_c=temp_c,
        precip_mm=precip_mm,
        wind_kmh=wind_kmh,
        cloud_pct=cloud_pct,
        fetched_at=FETCHED,
    )


def _seed_walks(session: Session) -> None:
    # Ben Vorlich has two future hours; An Teallach has no forecast at all.
    session.add(
        Walk(
            uuid=BEN,
            name="Ben Vorlich from Ardlui",
            url="https://www.walkhighlands.co.uk/lochlomond/ben-vorlich-ardlui.shtml",
            distance_km=11.0,
            ascent_m=920,
            duration_h=5.5,
            summary="A steep but rewarding Munro above Loch Lomond.",
            latitude=56.2734,
            longitude=-4.7521,
        )
    )
    session.add(
        Walk(
            uuid=TEALLACH,
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
    session.add(_hour(BEN, H1))
    session.add(_hour(BEN, H2, temp_c=15.0, wind_kmh=18.0, cloud_pct=55.0))
    session.commit()


class TestWalks:
    def test_payload_shape_and_ordering(self, client, session):
        _seed_walks(session)
        r = client.get("/api/hikes/walks")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 2
        assert body["generated_at"] == FETCHED.isoformat()
        assert len(body["walks"]) == 2

        # Ordered by name: "An Teallach circuit" before "Ben Vorlich from Ardlui".
        names = [w["name"] for w in body["walks"]]
        assert names == ["An Teallach circuit", "Ben Vorlich from Ardlui"]

        ben = body["walks"][1]
        assert ben["uuid"] == BEN
        assert ben["url"].endswith("ben-vorlich-ardlui.shtml")
        assert ben["distance_km"] == 11.0
        assert ben["ascent_m"] == 920
        assert ben["duration_h"] == 5.5
        assert ben["latitude"] == 56.2734
        assert ben["longitude"] == -4.7521
        # The light list carries viable_days, not the hourly windows or summary
        # (those live on the per-walk detail endpoint).
        assert ben["viable_days"] == _uk_days(H1, H2)
        assert "windows" not in ben
        assert "summary" not in ben

        # The walk without a forecast has no viable days.
        an_teallach = body["walks"][0]
        assert an_teallach["viable_days"] == []

    def test_elapsed_hours_excluded_from_viable_days(self, client, session):
        # An hour below the clock-hour cutoff must not contribute a viable day,
        # even though the prune job has not run.
        session.add(
            Walk(
                uuid=BEN,
                name="Ben Vorlich from Ardlui",
                url="https://example.invalid/ben.shtml",
                distance_km=11.0,
                ascent_m=920,
                duration_h=5.5,
                summary="",
                latitude=56.2734,
                longitude=-4.7521,
            )
        )
        session.add(_hour(BEN, ELAPSED))
        session.add(_hour(BEN, H1))
        session.commit()

        body = client.get("/api/hikes/walks").json()
        assert body["walks"][0]["viable_days"] == _uk_days(H1)

    def test_cache_and_etag_headers(self, client, session):
        _seed_walks(session)
        r = client.get("/api/hikes/walks")
        assert r.status_code == 200
        assert (
            r.headers["Cache-Control"]
            == "public, max-age=0, s-maxage=1800, stale-while-revalidate=3600, stale-if-error=86400"
        )
        # v3 schema token plus the hourly cutoff are folded into the ETag.
        assert r.headers["ETag"].startswith(f'"v3-{CUTOFF.isoformat()}-')

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


class TestWalkDetail:
    def test_detail_returns_summary_and_windows(self, client, session):
        _seed_walks(session)
        r = client.get(f"/api/hikes/walks/{BEN}")
        assert r.status_code == 200
        body = r.json()
        assert body["uuid"] == BEN
        assert body["summary"] == "A steep but rewarding Munro above Loch Lomond."
        # Hours reassembled into wire tuples, ordered by time. wind_kmh/cloud_pct
        # come back as ints (legacy tuple shape); temp_c/precip_mm stay numeric.
        assert body["windows"] == [
            [int(H1.timestamp()), 12.5, 0.0, 14, 40],
            [int(H2.timestamp()), 15.0, 0.0, 18, 55],
        ]
        assert r.headers["Cache-Control"]

    def test_detail_excludes_elapsed_hours(self, client, session):
        session.add(
            Walk(
                uuid=BEN,
                name="Ben Vorlich from Ardlui",
                url="https://example.invalid/ben.shtml",
                distance_km=11.0,
                ascent_m=920,
                duration_h=5.5,
                summary="",
                latitude=56.2734,
                longitude=-4.7521,
            )
        )
        session.add(_hour(BEN, ELAPSED))
        session.add(_hour(BEN, H1))
        session.commit()

        body = client.get(f"/api/hikes/walks/{BEN}").json()
        times = [w[0] for w in body["windows"]]
        assert times == [int(H1.timestamp())]

    def test_detail_404_for_unknown_uuid(self, client, session):
        _seed_walks(session)
        r = client.get("/api/hikes/walks/does-not-exist")
        assert r.status_code == 404
