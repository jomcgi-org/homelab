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

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from core.db import get_session
from shared.forecast_freshness import top_of_hour
import stars.router as stars_router
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
def client_fixture(session, monkeypatch):
    def read_materialized_sites():
        payload = stars_router._build_sites_from_db(
            SimpleNamespace(headers={}), Response(), session
        )
        return json.dumps(payload, separators=(",", ":")).encode(), '"test"'

    monkeypatch.setattr(
        stars_router, "_read_materialized_sites", read_materialized_sites
    )
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
        # No sites and no hours: total_sites reflects the empty stars.sites table,
        # and with no kept hours at all the darkness mode is "none".
        r = client.get("/api/stars/sites")
        assert r.status_code == 200
        assert r.json() == {
            "sites": [],
            "count": 0,
            "total_sites": 0,
            "darkness": "none",
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

    def test_best_hours_capped_at_display_limit(self, client, session):
        # 30 future clear-dark hours; the count reports all 30 but the displayed
        # list is capped at the earliest 25 by time. The surplus over the ~8 the
        # card shows is the headroom the client filters down from as hours elapse.
        _seed_site(session, "galloway-forest")
        session.add_all([_hour("galloway-forest", i + 1) for i in range(30)])
        session.commit()

        r = client.get("/api/stars/sites")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        site = body["sites"][0]
        assert site["clear_dark_hours"] == 30
        best_hours = site["best_hours"]
        assert len(best_hours) == 25
        # The earliest 25 hours by time (offsets 1..25).
        expected = [(CUTOFF + timedelta(hours=i + 1)).isoformat() for i in range(25)]
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

    def test_multi_night_hours_all_in_best_hours(self, client, session):
        # Two nights' worth of clear-dark hours are all carried in best_hours
        # (within the display cap), so the client can bucket them by night for
        # the filter chips. The per-night maps and the top-level nights union are
        # no longer computed server-side (the client derives them from these
        # windows), so the payload carries neither.
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
        site = body["sites"][0]
        assert site["clear_dark_hours"] == 3
        assert len(site["best_hours"]) == 3
        # The server no longer emits per-night maps or a nights union.
        assert "night_clear_dark" not in site
        assert "night_clear_twilight" not in site
        assert "nights" not in body

    def test_clear_twilight_at_least_clear_dark_and_dark_flag(self, client, session):
        # A clear true-dark hour (sun -18) plus a clear twilight-only hour
        # (sun -11). Twilight is the superset: clear_twilight_hours (2) >=
        # clear_dark_hours (1). best_hours carries both, each tagged dark.
        _seed_site(session, "galloway-forest")
        session.add(_hour("galloway-forest", 1, sun=-18.0))
        session.add(_hour("galloway-forest", 2, sun=-11.0))
        session.commit()

        r = client.get("/api/stars/sites")
        assert r.status_code == 200
        body = r.json()
        site = body["sites"][0]
        assert site["clear_dark_hours"] == 1
        assert site["clear_twilight_hours"] == 2
        assert site["clear_twilight_hours"] >= site["clear_dark_hours"]
        # best_hours shows the full clear-twilight superset, sorted by time, each
        # tagged dark (true at -18, false at -11).
        flags = {h["time"]: h["dark"] for h in site["best_hours"]}
        assert flags[(CUTOFF + timedelta(hours=1)).isoformat()] is True
        assert flags[(CUTOFF + timedelta(hours=2)).isoformat()] is False
        # Each window carries only the fields the card renders: time, cloud, dark.
        assert all(
            set(h) == {"time", "cloud_area_fraction", "dark"}
            for h in site["best_hours"]
        )

    def test_darkness_astronomical_when_any_dark_hour(self, client, session):
        # Any upcoming true-dark hour (sun < -12) anywhere puts the page in
        # astronomical mode, regardless of cloud (a dark-but-cloudy hour counts).
        _seed_site(session, "galloway-forest")
        session.add(_hour("galloway-forest", 1, sun=-13.0, cloud=80.0))
        session.commit()

        r = client.get("/api/stars/sites")
        assert r.status_code == 200
        body = r.json()
        assert body["darkness"] == "astronomical"

    def test_darkness_twilight_when_only_twilight_hours(self, client, session):
        # Midsummer: no hour reaches -12, but some are below the -10 floor. The
        # page falls back to twilight; clear_dark is zero while clear_twilight
        # carries the windows.
        _seed_site(session, "galloway-forest")
        session.add(_hour("galloway-forest", 1, sun=-11.0))
        session.add(_hour("galloway-forest", 2, sun=-10.5))
        session.commit()

        r = client.get("/api/stars/sites")
        assert r.status_code == 200
        body = r.json()
        assert body["darkness"] == "twilight"
        site = body["sites"][0]
        assert site["clear_dark_hours"] == 0
        assert site["clear_twilight_hours"] == 2

    def test_darkness_none_when_no_kept_hours(self, client, session):
        # A site exists but only has past hours -> no kept rows at all -> the
        # page reports darkness "none" (not even twilight is available).
        _seed_site(session, "galloway-forest")
        session.add(_hour("galloway-forest", -1, sun=-18.0))
        session.commit()

        r = client.get("/api/stars/sites")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 0
        assert body["darkness"] == "none"


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

    def test_all_months_per_site_arrays_sorted(self, client, session):
        # The bulk payload now carries every site's full 12-month breakdown as a
        # `clear` array (clear-dark hours, index 0 = January) and a matching
        # `dark` array, so the frontend can filter to any month client-side.
        _seed_site(session, "galloway-forest")
        _seed_site(session, "tomintoul")
        self._seed_climo(session, "galloway-forest", 1, 30, 8)
        self._seed_climo(session, "galloway-forest", 12, 50, 25)
        self._seed_climo(session, "tomintoul", 6, 10, 9)
        session.commit()

        r = client.get("/api/stars/history")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 2
        assert "month" not in body
        # Ordered by all-year clear-dark hours descending: galloway (33) leads
        # tomintoul (9).
        assert [s["id"] for s in body["sites"]] == ["galloway-forest", "tomintoul"]

        gf = body["sites"][0]
        # Metadata joined from stars.sites.
        assert gf["name"] == "Galloway Forest Park"
        assert gf["lat"] == 55.083
        # 12-element arrays, indexed Jan..Dec, zero-filled for absent months.
        assert len(gf["clear"]) == 12
        assert len(gf["dark"]) == 12
        assert gf["clear"][0] == 8  # January
        assert gf["clear"][11] == 25  # December
        assert gf["dark"][0] == 30
        assert gf["dark"][11] == 50
        assert gf["clear"][5] == 0  # June: no row
        # Per-site month buckets do not bleed across sites.
        tom = body["sites"][1]
        assert tom["clear"][5] == 9  # June
        assert tom["dark"][5] == 10
        assert sum(tom["clear"]) == 9

    def test_site_with_no_dark_hours_in_any_month_is_omitted(self, client, session):
        # A site whose only climatology row has zero dark hours has nothing to
        # show in any view and is omitted (never a divide-by-zero on the client).
        _seed_site(session, "galloway-forest")
        self._seed_climo(session, "galloway-forest", 5, 0, 0)
        session.commit()

        r = client.get("/api/stars/history")
        assert r.status_code == 200
        assert r.json() == {"sites": [], "count": 0}

    def test_orphan_climatology_without_site_is_skipped(self, client, session):
        # A climatology row whose site dropped from the grid has no metadata to
        # render and is omitted.
        self._seed_climo(session, "ghost", 12, 5, 4)
        session.commit()

        r = client.get("/api/stars/history")
        assert r.status_code == 200
        assert r.json() == {"sites": [], "count": 0}

    def test_cache_and_etag_headers(self, client, session):
        _seed_site(session, "galloway-forest")
        self._seed_climo(session, "galloway-forest", 12, 10, 5)
        session.commit()
        r = client.get("/api/stars/history")
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
        first = client.get("/api/stars/history")
        etag = first.headers["ETag"]
        second = client.get(
            "/api/stars/history",
            headers={"If-None-Match": etag},
        )
        assert second.status_code == 304
        assert second.headers["ETag"] == etag
        assert second.headers["Cache-Control"]
        assert second.content == b""

    def test_empty_climatology_returns_empty(self, client, session):
        _seed_site(session, "galloway-forest")
        session.commit()
        r = client.get("/api/stars/history")
        assert r.status_code == 200
        assert r.json() == {"sites": [], "count": 0}
