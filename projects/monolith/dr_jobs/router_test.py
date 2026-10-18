"""Unit tests for dr_jobs/router.py: /api/dr-jobs/listings.

File-backed SQLite seeded with live, closed, and aged-out rows, mounted on a
minimal FastAPI app via app.dependency_overrides[get_session], mirroring
hikes/router_test. Asserts the server-computed is_live split, the live/history
ordering, and the ETag/304 path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

import dr_jobs.router as jobs_router
from core.db import get_session
from dr_jobs.models import Vacancy
from dr_jobs.router import router

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
TODAY = NOW.date()
STALE_SEEN = NOW - timedelta(hours=4)  # beyond three hourly scrape intervals


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(jobs_router, "datetime", FixedDatetime)


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'vacancies.db'}",
        connect_args={"check_same_thread": False},
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
        engine.dispose()
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


def _vac(job_id, *, title, closing, last_seen=NOW, town="Elgin"):
    return Vacancy(
        job_id=job_id,
        title=title,
        town=town,
        closing_date=closing,
        url=f"https://apply.jobs.scot.nhs.uk/Job/JobDetail?JobId={job_id}",
        first_seen_at=NOW,
        last_seen_at=last_seen,
        scraped_at=last_seen,
    )


def _seed(session):
    # Live: seen now, closes in the future (soonest first after sort).
    session.add(_vac("A", title="Live soon", closing=TODAY + timedelta(days=2)))
    session.add(_vac("B", title="Live later", closing=TODAY + timedelta(days=20)))
    # History: seen now but past its closing date.
    session.add(_vac("C", title="Closed", closing=TODAY - timedelta(days=3)))
    # History: still open but not seen in the last scrape (aged out).
    session.add(
        _vac(
            "D", title="Stale", closing=TODAY + timedelta(days=5), last_seen=STALE_SEEN
        )
    )
    session.commit()


class TestListings:
    def test_live_grace_is_three_hourly_intervals(self):
        assert jobs_router.LIVE_GRACE == timedelta(hours=3)

    @pytest.mark.parametrize(
        ("age", "within_grace"),
        [
            (timedelta(hours=3) - timedelta(microseconds=1), True),
            (timedelta(hours=3), True),
            (timedelta(hours=3) + timedelta(microseconds=1), False),
        ],
        ids=["just-inside", "exact-cutoff", "just-outside"],
    )
    @pytest.mark.parametrize(
        ("closing", "still_open"),
        [
            (None, True),
            (TODAY + timedelta(days=1), True),
            (TODAY, True),
            (TODAY - timedelta(days=1), False),
        ],
        ids=["no-closing-date", "closes-tomorrow", "closes-today", "closed"],
    )
    def test_live_cutoff_is_inclusive(
        self, client, session, age, within_grace, closing, still_open
    ):
        session.add(
            _vac("BOUNDARY", title="Cutoff", closing=closing, last_seen=NOW - age)
        )
        session.commit()

        response = client.get("/api/dr-jobs/listings")
        assert response.status_code == 200
        body = response.json()
        expected_live = within_grace and still_open
        # Expired rows remain in the payload as history, including closed ones.
        assert body["count"] == 1
        assert body["live_count"] == int(expected_live)
        assert body["jobs"][0]["job_id"] == "BOUNDARY"
        assert body["jobs"][0]["is_live"] is expected_live

    def test_split_and_ordering(self, client, session):
        _seed(session)
        body = client.get("/api/dr-jobs/listings").json()
        assert body["count"] == 4
        assert body["live_count"] == 2

        ids = [j["job_id"] for j in body["jobs"]]
        # Live first (soonest closing: A before B), then history.
        assert ids[:2] == ["A", "B"]
        assert set(ids[2:]) == {"C", "D"}

        flags = {j["job_id"]: j["is_live"] for j in body["jobs"]}
        assert flags == {"A": True, "B": True, "C": False, "D": False}

    def test_serialized_fields(self, client, session):
        _seed(session)
        body = client.get("/api/dr-jobs/listings").json()
        a = next(j for j in body["jobs"] if j["job_id"] == "A")
        assert a["title"] == "Live soon"
        assert a["closing_date"] == (TODAY + timedelta(days=2)).isoformat()
        assert a["url"].endswith("JobId=A")
        assert a["is_live"] is True

    def test_cache_and_etag_headers(self, client, session):
        _seed(session)
        r = client.get("/api/dr-jobs/listings")
        assert r.status_code == 200
        assert (
            r.headers["Cache-Control"]
            == "public, max-age=0, s-maxage=1800, stale-while-revalidate=3600, stale-if-error=86400"
        )
        assert r.headers["ETag"].startswith(f'"v1-{TODAY.isoformat()}-')

    def test_conditional_get_returns_304(self, client, session):
        _seed(session)
        etag = client.get("/api/dr-jobs/listings").headers["ETag"]
        second = client.get("/api/dr-jobs/listings", headers={"If-None-Match": etag})
        assert second.status_code == 304
        assert second.headers["ETag"] == etag

    def test_empty(self, client):
        body = client.get("/api/dr-jobs/listings").json()
        assert body == {
            "count": 0,
            "live_count": 0,
            "generated_at": None,
            "jobs": [],
        }

    def test_null_closing_date_is_live_when_recently_seen(self, client, session):
        session.add(_vac("E", title="No closing", closing=None))
        session.commit()
        body = client.get("/api/dr-jobs/listings").json()
        assert body["live_count"] == 1
        assert body["jobs"][0]["is_live"] is True
        assert body["jobs"][0]["closing_date"] is None
