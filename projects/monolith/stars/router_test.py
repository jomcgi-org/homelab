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
from stars.models import Site, SiteHour, SiteMonthClimatology
from stars.router import _HISTORY_CACHE_CONTROL, _SITES_CACHE_CONTROL, router

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
    cloud: float = 5.0,
    sun: float = -18.0,
) -> SiteHour:
    """A SiteHour for ``site_id`` at cutoff + offset_hours.

    Defaults are a clear-dark hour (sun below -12, cloud below 10%); pass a
    higher ``cloud`` to make the dark hour not-clear.
    """
    return SiteHour(
        site_id=site_id,
        hour_time=CUTOFF + timedelta(hours=offset_hours),
        cloud_area_fraction=cloud,
        air_temperature=8.0,
        dew_spread=2.0,
        sun_elevation_deg=sun,
        symbol="clearsky_night",
        fetched_at=FETCHED,
    )


class TestSites:
    def test_ordering_by_clear_dark_hours(self, client, session):
        # galloway-forest has 2 upcoming clear-dark hours; tomintoul has 1 (its
        # second future hour is dark but cloudy).
        _seed_site(session, "galloway-forest")
        _seed_site(session, "tomintoul")
        session.add(_hour("galloway-forest", 1))
        session.add(_hour("galloway-forest", 2))
        session.add(_hour("tomintoul", 1))
        session.add(_hour("tomintoul", 2, cloud=50.0))
        session.commit()

        r = client.get("/api/stars/sites")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 2
        ids = [s["id"] for s in body["sites"]]
        assert ids == ["galloway-forest", "tomintoul"]
        assert body["sites"][0]["clear_dark_hours"] == 2
        assert body["sites"][1]["clear_dark_hours"] == 1
        # Metadata is joined in from the stars.sites table.
        assert body["sites"][0]["name"] == "Galloway Forest Park"
        assert body["sites"][0]["lp_zone"] == "1a"

    def test_past_hours_omitted(self, client, session):
        # galloway-forest has only past hours -> excluded entirely.
        _seed_site(session, "galloway-forest")
        _seed_site(session, "tomintoul")
        session.add(_hour("galloway-forest", -1))
        session.add(_hour("galloway-forest", -2))
        # tomintoul has a mix: only its future clear-dark hours are counted.
        session.add(_hour("tomintoul", -1))
        session.add(_hour("tomintoul", 1))
        session.add(_hour("tomintoul", 2))
        session.commit()

        r = client.get("/api/stars/sites")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert [s["id"] for s in body["sites"]] == ["tomintoul"]
        tomintoul = body["sites"][0]
        # Only the two future hours; the past clear-dark hour is gone.
        assert len(tomintoul["best_hours"]) == 2
        assert tomintoul["clear_dark_hours"] == 2
        future_times = {
            (CUTOFF + timedelta(hours=1)).isoformat(),
            (CUTOFF + timedelta(hours=2)).isoformat(),
        }
        assert {h["time"] for h in tomintoul["best_hours"]} == future_times

    def test_dark_but_cloudy_site_has_zero_clear_dark(self, client, session):
        # A dark site whose upcoming hours are all cloudy still appears, with a
        # zero clear-dark count and no displayed windows.
        _seed_site(session, "galloway-forest")
        session.add(_hour("galloway-forest", 1, cloud=80.0))
        session.add(_hour("galloway-forest", 2, cloud=90.0))
        session.commit()

        r = client.get("/api/stars/sites")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        gf = body["sites"][0]
        assert gf["clear_dark_hours"] == 0
        assert gf["best_hours"] == []

    def test_cache_and_etag_headers(self, client, session):
        _seed_site(session, "galloway-forest")
        session.add(_hour("galloway-forest", 1))
        session.commit()
        r = client.get("/api/stars/sites")
        assert r.status_code == 200
        assert r.headers["Cache-Control"] == _SITES_CACHE_CONTROL
        assert r.headers["ETag"]

    def test_conditional_get_returns_304(self, client, session):
        _seed_site(session, "galloway-forest")
        session.add(_hour("galloway-forest", 1))
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
        session.add(_hour("galloway-forest", 1))
        session.commit()

        r = client.get("/api/stars/sites")
        body = r.json()
        assert body["count"] == 1
        assert body["total_sites"] == 2

    def test_best_hours_capped_at_eight(self, client, session):
        # 12 future clear-dark hours; the count reports all 12 but the displayed
        # list is capped at the earliest 8 by time.
        _seed_site(session, "galloway-forest")
        session.add_all([_hour("galloway-forest", i + 1) for i in range(12)])
        session.commit()

        r = client.get("/api/stars/sites")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        site = body["sites"][0]
        assert site["clear_dark_hours"] == 12
        best_hours = site["best_hours"]
        assert len(best_hours) == 8
        # The earliest 8 hours by time (offsets 1..8).
        expected = [(CUTOFF + timedelta(hours=i + 1)).isoformat() for i in range(8)]
        assert [h["time"] for h in best_hours] == expected

    def test_best_hours_in_chronological_order(self, client, session):
        # The card must read by time, so the response sorts the displayed hours
        # ascending by hour_time.
        _seed_site(session, "galloway-forest")
        session.add(_hour("galloway-forest", 2))
        session.add(_hour("galloway-forest", 1))
        session.add(_hour("galloway-forest", 3))
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

    def test_night_clear_dark_and_nights(self, client, session):
        # Two nights' worth of clear-dark hours: each site exposes the per-night
        # clear-dark count (keyed by the evening date), and the response lists
        # the union of nights ascending for the filter chips.
        _seed_site(session, "galloway-forest")
        # Offsets chosen relative to a fixed cutoff so both nights are covered
        # regardless of when the suite runs.
        session.add(_hour("galloway-forest", 1))
        session.add(_hour("galloway-forest", 2))
        session.add(_hour("galloway-forest", 26))
        session.commit()

        r = client.get("/api/stars/sites")
        assert r.status_code == 200
        body = r.json()
        night_clear_dark = body["sites"][0]["night_clear_dark"]
        # The per-night counts sum to the site's total clear-dark hours.
        assert sum(night_clear_dark.values()) == 3
        # Top-level nights is the sorted union of the per-site night keys.
        assert body["nights"] == sorted(set(night_clear_dark))
        assert body["nights"] == sorted(body["nights"])


class TestHistory:
    def _seed_climo(self, session, site_id, month, dark_hours, clear_dark_hours):
        session.add(
            SiteMonthClimatology(
                site_id=site_id,
                month=month,
                dark_hours=dark_hours,
                clear_dark_hours=clear_dark_hours,
            )
        )

    def test_month_filter_ordering_rate_no_months_in_bulk(self, client, session):
        _seed_site(session, "galloway-forest")
        _seed_site(session, "tomintoul")
        # December: the historical layer reads the ERA5/CERRA climatology only.
        # galloway leads on clear_dark_hours.
        self._seed_climo(session, "galloway-forest", 12, 50, 25)
        self._seed_climo(session, "tomintoul", 12, 25, 6)
        # A different month is excluded from the headline counts entirely.
        self._seed_climo(session, "tomintoul", 6, 10, 10)
        session.commit()

        r = client.get("/api/stars/history", params={"month": 12})
        assert r.status_code == 200
        body = r.json()
        assert body["month"] == 12
        assert body["count"] == 2
        ids = [s["id"] for s in body["sites"]]
        # Ordered by clear_dark_hours descending.
        assert ids == ["galloway-forest", "tomintoul"]
        gf = body["sites"][0]
        assert gf["clear_dark_hours"] == 25
        assert gf["dark_hours"] == 50
        assert gf["clear_rate"] == pytest.approx(25 / 50)
        # Metadata joined from stars.sites.
        assert gf["name"] == "Galloway Forest Park"
        assert gf["lat"] == 55.083
        # The bulk payload is slim: no per-month map (it is fetched lazily per
        # site from /history/site/{id}).
        assert "months" not in gf
        # tomintoul's December headline excludes its June bucket.
        tom = body["sites"][1]
        assert tom["clear_dark_hours"] == 6
        assert "months" not in tom

    def test_default_is_all_year(self, client, session):
        # No month param -> all-year view (month 0): every month-of-year bucket
        # for a site is summed into one row.
        _seed_site(session, "galloway-forest")
        _seed_site(session, "tomintoul")
        self._seed_climo(session, "galloway-forest", 1, 10, 8)
        self._seed_climo(session, "galloway-forest", 12, 20, 15)
        self._seed_climo(session, "galloway-forest", 6, 5, 4)
        self._seed_climo(session, "tomintoul", 7, 8, 6)
        session.commit()

        r = client.get("/api/stars/history")
        assert r.status_code == 200
        body = r.json()
        assert body["month"] == 0
        gf = next(s for s in body["sites"] if s["id"] == "galloway-forest")
        # All three galloway rows (Jan + Dec + Jun) fold into one.
        assert gf["dark_hours"] == 10 + 20 + 5
        assert gf["clear_dark_hours"] == 8 + 15 + 4
        assert "months" not in gf
        # Ordered by clear_dark_hours desc: galloway (27) before tomintoul (6).
        assert [s["id"] for s in body["sites"]] == ["galloway-forest", "tomintoul"]

    def test_clear_rate_guards_zero_dark(self, client, session):
        # A site whose only climatology row has zero dark hours has no dark hours
        # to show and would divide by zero, so it is omitted (never a NaN rate).
        _seed_site(session, "galloway-forest")
        self._seed_climo(session, "galloway-forest", 5, 0, 0)
        session.commit()

        r = client.get("/api/stars/history", params={"month": 5})
        assert r.status_code == 200
        assert r.json() == {"month": 5, "sites": [], "count": 0}

    def test_orphan_climatology_without_site_is_skipped(self, client, session):
        # A climatology row whose site dropped from the grid has no metadata to
        # render and is omitted.
        self._seed_climo(session, "ghost", 12, 5, 4)
        session.commit()

        r = client.get("/api/stars/history", params={"month": 12})
        assert r.status_code == 200
        assert r.json() == {"month": 12, "sites": [], "count": 0}

    def test_cache_and_etag_headers(self, client, session):
        _seed_site(session, "galloway-forest")
        self._seed_climo(session, "galloway-forest", 12, 10, 5)
        session.commit()
        r = client.get("/api/stars/history", params={"month": 12})
        assert r.status_code == 200
        # History caches longer than the live sites layer (it is effectively
        # static between climatology reloads).
        assert r.headers["Cache-Control"] == _HISTORY_CACHE_CONTROL
        assert _HISTORY_CACHE_CONTROL != _SITES_CACHE_CONTROL
        assert r.headers["ETag"]

    def test_conditional_get_returns_304(self, client, session):
        _seed_site(session, "galloway-forest")
        self._seed_climo(session, "galloway-forest", 12, 10, 5)
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


class TestHistorySite:
    def _seed_climo(self, session, site_id, month, dark_hours, clear_dark_hours):
        session.add(
            SiteMonthClimatology(
                site_id=site_id,
                month=month,
                dark_hours=dark_hours,
                clear_dark_hours=clear_dark_hours,
            )
        )

    def test_returns_zero_filled_12_month_map(self, client, session):
        # The per-site breakdown is a {1..12: clear_dark_hours} map (string keys
        # after JSON), zero-filled for months with no climatology row.
        self._seed_climo(session, "galloway-forest", 1, 30, 8)
        self._seed_climo(session, "galloway-forest", 12, 50, 25)
        # A different site's rows must not bleed into this one.
        self._seed_climo(session, "tomintoul", 6, 10, 9)
        session.commit()

        r = client.get("/api/stars/history/site/galloway-forest")
        assert r.status_code == 200
        body = r.json()
        assert body["site_id"] == "galloway-forest"
        months = body["months"]
        assert set(months.keys()) == {str(m) for m in range(1, 13)}
        assert months["1"] == 8
        assert months["12"] == 25
        assert months["6"] == 0

    def test_unknown_site_returns_all_zero_map(self, client, session):
        # A site with no climatology rows (or no metadata) still returns a valid
        # all-zero 12-month map rather than 404, so the card chart renders empty.
        r = client.get("/api/stars/history/site/ghost")
        assert r.status_code == 200
        body = r.json()
        assert body["site_id"] == "ghost"
        assert body["months"] == {str(m): 0 for m in range(1, 13)}

    def test_cache_and_etag_headers(self, client, session):
        self._seed_climo(session, "galloway-forest", 12, 10, 5)
        session.commit()
        r = client.get("/api/stars/history/site/galloway-forest")
        assert r.status_code == 200
        assert r.headers["Cache-Control"] == _HISTORY_CACHE_CONTROL
        assert r.headers["ETag"]

    def test_conditional_get_returns_304(self, client, session):
        self._seed_climo(session, "galloway-forest", 12, 10, 5)
        session.commit()
        first = client.get("/api/stars/history/site/galloway-forest")
        etag = first.headers["ETag"]
        second = client.get(
            "/api/stars/history/site/galloway-forest",
            headers={"If-None-Match": etag},
        )
        assert second.status_code == 304
        assert second.headers["ETag"] == etag
        assert second.content == b""
