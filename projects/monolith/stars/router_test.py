"""Unit tests for stars/router.py: /api/stars/sites.

Uses an in-memory SQLite DB seeded with stars.sites rows + site_hours and a
minimal FastAPI app that mounts only the stars router, mirroring the
schema-stripping + ``app.dependency_overrides[get_session]`` pattern in
``ships/router_test.py`` and ``hikes/router_test.py``.

Timestamps are computed relative to ``top_of_hour(datetime.now(timezone.utc))``
so the read-time ``hour_time >= cutoff`` filter behaves the same regardless of
when the suite runs (no hour-boundary flakiness).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.db import get_session
from shared.forecast_freshness import top_of_hour
from stars.models import Site, SiteHour, SiteMonthStat
from stars.router import _SITES_CACHE_CONTROL, router

CUTOFF = top_of_hour(datetime.now(timezone.utc))
FETCHED = CUTOFF - timedelta(minutes=10)

# Site metadata the router joins in from stars.sites, keyed by id.
_SITE_META = {
    "galloway-forest": ("Galloway Forest Park", 55.083, -4.500, 110, "1a"),
    "tomintoul": ("Tomintoul", 57.249, -3.371, 345, "1a"),
}


def _seed_site(session, site_id):
    name, lat, lon, alt, lp = _SITE_META[site_id]
    session.add(
        Site(
            id=site_id,
            name=name,
            lat=lat,
            lon=lon,
            altitude_m=alt,
            lp_zone=lp,
            source="grid",
        )
    )


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
    site_id: str,
    offset_hours: int,
    score: float,
) -> SiteHour:
    """A SiteHour for ``site_id`` at cutoff + offset_hours, with a given score."""
    return SiteHour(
        site_id=site_id,
        hour_time=CUTOFF + timedelta(hours=offset_hours),
        score=score,
        cloud_area_fraction=10.0,
        relative_humidity=70.0,
        wind_speed=3.0,
        air_temperature=8.0,
        dew_spread=2.0,
        symbol="clearsky_night",
        fetched_at=FETCHED,
    )


class TestSites:
    def test_ordering_by_best_score(self, client, session):
        # galloway-forest peaks at 0.9; tomintoul peaks at 0.5.
        _seed_site(session, "galloway-forest")
        _seed_site(session, "tomintoul")
        session.add(_hour("galloway-forest", 1, 0.7))
        session.add(_hour("galloway-forest", 2, 0.9))
        session.add(_hour("tomintoul", 1, 0.5))
        session.add(_hour("tomintoul", 2, 0.3))
        session.commit()

        r = client.get("/api/stars/sites")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 2
        ids = [s["id"] for s in body["sites"]]
        assert ids == ["galloway-forest", "tomintoul"]
        assert body["sites"][0]["best_score"] == 0.9
        # Metadata is joined in from the stars.sites table.
        assert body["sites"][0]["name"] == "Galloway Forest Park"
        assert body["sites"][0]["lp_zone"] == "1a"

    def test_past_hours_omitted(self, client, session):
        # galloway-forest has only past hours -> excluded entirely.
        _seed_site(session, "galloway-forest")
        _seed_site(session, "tomintoul")
        session.add(_hour("galloway-forest", -1, 0.9))
        session.add(_hour("galloway-forest", -2, 0.8))
        # tomintoul has a mix: only its future hours come back.
        session.add(_hour("tomintoul", -1, 0.95))
        session.add(_hour("tomintoul", 1, 0.4))
        session.add(_hour("tomintoul", 2, 0.6))
        session.commit()

        r = client.get("/api/stars/sites")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert [s["id"] for s in body["sites"]] == ["tomintoul"]
        tomintoul = body["sites"][0]
        # Only the two future hours; the past hour (and its higher score) is gone.
        assert len(tomintoul["best_hours"]) == 2
        assert tomintoul["best_score"] == 0.6
        future_times = {
            (CUTOFF + timedelta(hours=1)).isoformat(),
            (CUTOFF + timedelta(hours=2)).isoformat(),
        }
        assert {h["time"] for h in tomintoul["best_hours"]} == future_times

    def test_cache_and_etag_headers(self, client, session):
        _seed_site(session, "galloway-forest")
        session.add(_hour("galloway-forest", 1, 0.7))
        session.commit()
        r = client.get("/api/stars/sites")
        assert r.status_code == 200
        assert r.headers["Cache-Control"] == _SITES_CACHE_CONTROL
        assert r.headers["ETag"]

    def test_conditional_get_returns_304(self, client, session):
        _seed_site(session, "galloway-forest")
        session.add(_hour("galloway-forest", 1, 0.7))
        session.commit()
        first = client.get("/api/stars/sites")
        etag = first.headers["ETag"]
        second = client.get("/api/stars/sites", headers={"If-None-Match": etag})
        assert second.status_code == 304
        assert second.headers["ETag"] == etag
        assert second.headers["Cache-Control"]
        assert second.content == b""

    def test_empty_table(self, client):
        # No sites and no hours: total_sites reflects the empty stars.sites table.
        r = client.get("/api/stars/sites")
        assert r.status_code == 200
        assert r.json() == {
            "sites": [],
            "count": 0,
            "total_sites": 0,
            "nights": [],
            "fetched_at": None,
        }

    def test_total_sites_counts_stars_sites_rows(self, client, session):
        # total_sites is the count of stars.sites rows, independent of which
        # sites have upcoming hours.
        _seed_site(session, "galloway-forest")
        _seed_site(session, "tomintoul")
        session.add(_hour("galloway-forest", 1, 0.7))
        session.commit()

        r = client.get("/api/stars/sites")
        body = r.json()
        assert body["count"] == 1
        assert body["total_sites"] == 2

    def test_best_hours_capped_at_eight(self, client, session):
        # 12 future hours with ascending scores; expect the top 8 by score.
        _seed_site(session, "galloway-forest")
        session.add_all(
            [_hour("galloway-forest", i + 1, score=i / 100.0) for i in range(12)]
        )
        session.commit()

        r = client.get("/api/stars/sites")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        best_hours = body["sites"][0]["best_hours"]
        assert len(best_hours) == 8
        scores = [h["score"] for h in best_hours]
        # Selection is the 8 highest scores (offsets 5..12, scores 0.04..0.11).
        assert set(scores) == {0.11, 0.10, 0.09, 0.08, 0.07, 0.06, 0.05, 0.04}

    def test_best_hours_in_chronological_order(self, client, session):
        # Hours arrive scored out of time order; the card must read by time, so
        # the response sorts the selected hours ascending by hour_time.
        _seed_site(session, "galloway-forest")
        session.add(_hour("galloway-forest", 1, 0.3))
        session.add(_hour("galloway-forest", 2, 0.9))
        session.add(_hour("galloway-forest", 3, 0.5))
        session.commit()

        r = client.get("/api/stars/sites")
        assert r.status_code == 200
        body = r.json()
        times = [h["time"] for h in body["sites"][0]["best_hours"]]
        assert times == sorted(times)
        assert times == [
            (CUTOFF + timedelta(hours=1)).isoformat(),
            (CUTOFF + timedelta(hours=2)).isoformat(),
            (CUTOFF + timedelta(hours=3)).isoformat(),
        ]

    def test_night_scores_and_nights(self, client, session):
        # Two nights' worth of hours: each site exposes the best score per night
        # (keyed by the evening date), and the response lists the union of
        # nights ascending for the filter chips.
        _seed_site(session, "galloway-forest")
        # Offsets chosen relative to a fixed cutoff so both nights are covered
        # regardless of when the suite runs.
        session.add(_hour("galloway-forest", 1, 0.4))
        session.add(_hour("galloway-forest", 2, 0.8))
        session.add(_hour("galloway-forest", 26, 0.6))
        session.commit()

        r = client.get("/api/stars/sites")
        assert r.status_code == 200
        body = r.json()
        night_scores = body["sites"][0]["night_scores"]
        # Each night maps to the best score reached that night.
        assert max(night_scores.values()) == 0.8
        # Top-level nights is the sorted union of the per-site night keys.
        assert body["nights"] == sorted(set(night_scores))
        assert body["nights"] == sorted(body["nights"])


class TestHistory:
    def _seed_stat(
        self,
        session,
        site_id,
        month,
        window_count,
        sum_q,
        sum_darkness,
        sum_clarity,
    ):
        session.add(
            SiteMonthStat(
                site_id=site_id,
                month=month,
                window_count=window_count,
                sum_q=sum_q,
                sum_darkness=sum_darkness,
                sum_clarity=sum_clarity,
            )
        )

    def test_month_filter_ordering_and_derived_averages(self, client, session):
        _seed_site(session, "galloway-forest")
        _seed_site(session, "tomintoul")
        # December stats: galloway leads on sum_q.
        self._seed_stat(session, "galloway-forest", 12, 40, 3200.0, 36.0, 30.0)
        self._seed_stat(session, "tomintoul", 12, 20, 1000.0, 14.0, 8.0)
        # A different month must be excluded by the filter.
        self._seed_stat(session, "tomintoul", 6, 5, 5000.0, 4.0, 4.0)
        session.commit()

        r = client.get("/api/stars/history", params={"month": 12})
        assert r.status_code == 200
        body = r.json()
        assert body["month"] == 12
        assert body["count"] == 2
        ids = [s["id"] for s in body["sites"]]
        # Ordered by sum_q descending.
        assert ids == ["galloway-forest", "tomintoul"]
        gf = body["sites"][0]
        assert gf["sum_q"] == 3200.0
        assert gf["window_count"] == 40
        # Averages are the component sums over window_count.
        assert gf["avg_darkness"] == pytest.approx(36.0 / 40)
        assert gf["avg_clarity"] == pytest.approx(30.0 / 40)
        # Metadata joined from stars.sites.
        assert gf["name"] == "Galloway Forest Park"
        assert gf["lat"] == 55.083

    def test_default_month_is_current_utc(self, client, session):
        current = datetime.now(timezone.utc).month
        _seed_site(session, "galloway-forest")
        self._seed_stat(session, "galloway-forest", current, 10, 500.0, 9.0, 8.0)
        session.commit()

        r = client.get("/api/stars/history")
        assert r.status_code == 200
        body = r.json()
        assert body["month"] == current
        assert [s["id"] for s in body["sites"]] == ["galloway-forest"]

    def test_orphan_stat_without_site_is_skipped(self, client, session):
        # A site_month_stats row whose site dropped from the grid has no
        # metadata to render and is omitted.
        self._seed_stat(session, "ghost", 12, 5, 100.0, 4.0, 3.0)
        session.commit()

        r = client.get("/api/stars/history", params={"month": 12})
        assert r.status_code == 200
        assert r.json() == {"month": 12, "sites": [], "count": 0}

    def test_cache_and_etag_headers(self, client, session):
        _seed_site(session, "galloway-forest")
        self._seed_stat(session, "galloway-forest", 12, 10, 500.0, 9.0, 8.0)
        session.commit()
        r = client.get("/api/stars/history", params={"month": 12})
        assert r.status_code == 200
        assert r.headers["Cache-Control"] == _SITES_CACHE_CONTROL
        assert r.headers["ETag"]

    def test_conditional_get_returns_304(self, client, session):
        _seed_site(session, "galloway-forest")
        self._seed_stat(session, "galloway-forest", 12, 10, 500.0, 9.0, 8.0)
        session.commit()
        first = client.get("/api/stars/history", params={"month": 12})
        etag = first.headers["ETag"]
        second = client.get(
            "/api/stars/history",
            params={"month": 12},
            headers={"If-None-Match": etag},
        )
        assert second.status_code == 304
        assert second.headers["ETag"] == etag
        assert second.headers["Cache-Control"]
        assert second.content == b""

    def test_empty_month_returns_empty(self, client, session):
        _seed_site(session, "galloway-forest")
        session.commit()
        r = client.get("/api/stars/history", params={"month": 3})
        assert r.status_code == 200
        assert r.json() == {"month": 3, "sites": [], "count": 0}

    def test_invalid_month_rejected(self, client):
        assert client.get("/api/stars/history", params={"month": 13}).status_code == 422
        assert client.get("/api/stars/history", params={"month": -1}).status_code == 422
