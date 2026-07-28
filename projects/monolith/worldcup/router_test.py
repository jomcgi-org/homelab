"""Unit tests for worldcup/router.py: /api/wc2026/summary.

In-memory SQLite seeded with a realistic Group C scenario, mounted on a minimal
FastAPI app via app.dependency_overrides[get_session], mirroring
dr_jobs/router_test. Asserts the all-country payload shape (group tables,
qualification map, per-country swing map), the group-table sort, the swing
ordering, and the ETag/304 path.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from core.db import get_session
from worldcup.models import Qualification, Standing, SwingMatch
from worldcup.router import router

NOW = datetime.now(timezone.utc)


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
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _standing(team_id, name, code, group, *, pts, gf, ga, mp, w, d, l):
    return Standing(
        team_id=team_id,
        name=name,
        fifa_code=code,
        flag_url=f"https://flags.example/{code}.svg",
        group_name=group,
        mp=mp,
        w=w,
        d=d,
        l=l,
        pts=pts,
        gf=gf,
        ga=ga,
        gd=gf - ga,
        updated_at=NOW,
    )


def _seed(session):
    # Group C: Brazil, Morocco, Scotland, Haiti. Brazil and Morocco tie on
    # points (7) so goal difference breaks them; Scotland and Haiti below.
    session.add(
        _standing("bra", "Brazil", "BRA", "C", pts=7, gf=6, ga=2, mp=3, w=2, d=1, l=0)
    )
    session.add(
        _standing("mar", "Morocco", "MAR", "C", pts=7, gf=4, ga=1, mp=3, w=2, d=1, l=0)
    )
    session.add(
        _standing("sco", "Scotland", "SCO", "C", pts=4, gf=3, ga=3, mp=3, w=1, d=1, l=1)
    )
    session.add(
        _standing("hai", "Haiti", "HAI", "C", pts=1, gf=1, ga=8, mp=3, w=0, d=1, l=2)
    )
    # A team in another group to prove group filtering.
    session.add(
        _standing(
            "arg", "Argentina", "ARG", "A", pts=9, gf=8, ga=1, mp=3, w=3, d=0, l=0
        )
    )

    session.add(
        Qualification(
            team_id="sco",
            fifa_code="SCO",
            prob_qualify=0.62,
            prob_top2=0.41,
            prob_third=0.21,
            status="contention",
            n_sims=20000,
            computed_at=NOW,
        )
    )

    # Scotland's two swing rows (the focus by default).
    session.add(
        SwingMatch(
            match_id="m-bra-mar",
            country_code="SCO",
            group_name="C",
            home_code="BRA",
            away_code="MAR",
            kickoff=NOW,
            swing=0.18,
            p_qualify_home_win=0.70,
            p_qualify_draw=0.62,
            p_qualify_away_win=0.52,
            is_own_match=False,
        )
    )
    session.add(
        SwingMatch(
            match_id="m-sco-hai",
            country_code="SCO",
            group_name="C",
            home_code="SCO",
            away_code="HAI",
            kickoff=NOW,
            swing=0.44,
            p_qualify_home_win=0.85,
            p_qualify_draw=0.55,
            p_qualify_away_win=0.20,
            is_own_match=True,
        )
    )
    # A different country's swing on the same match, to prove per-country grouping.
    session.add(
        SwingMatch(
            match_id="m-bra-mar",
            country_code="BRA",
            group_name="C",
            home_code="BRA",
            away_code="MAR",
            kickoff=NOW,
            swing=0.33,
            p_qualify_home_win=0.95,
            p_qualify_draw=0.80,
            p_qualify_away_win=0.62,
            is_own_match=True,
        )
    )
    session.commit()


class TestSummary:
    def test_default_country_and_groups(self, client, session):
        _seed(session)
        body = client.get("/api/wc2026/summary").json()
        assert body["default_country"] == "SCO"
        # Every group present (C and the lone Group A team).
        assert set(body["groups"]) == {"C", "A"}
        assert body["n_sims"] == 20000
        assert body["updated_at"] is not None

    def test_group_tables_sorted_and_complete(self, client, session):
        _seed(session)
        body = client.get("/api/wc2026/summary").json()
        codes = [r["fifa_code"] for r in body["groups"]["C"]]
        assert codes == ["BRA", "MAR", "SCO", "HAI"]
        assert [r["fifa_code"] for r in body["groups"]["A"]] == ["ARG"]
        # Brazil ahead of Morocco on goal difference (4 vs 3) at equal points.
        bra = body["groups"]["C"][0]
        assert bra["pts"] == 7 and bra["gd"] == 4
        assert bra["name"] == "Brazil"
        assert bra["flag_url"].endswith("BRA.svg")

    def test_qualification_map(self, client, session):
        _seed(session)
        body = client.get("/api/wc2026/summary").json()
        # Keyed by fifa_code; raw 0..1 floats (not pre-multiplied).
        sco = body["qualification"]["SCO"]
        assert sco["prob_qualify"] == 0.62
        assert sco["prob_top2"] == 0.41
        assert sco["prob_third"] == 0.21
        assert sco["status"] == "contention"
        # A team with no qualification row yet falls back to a sane default.
        bra = body["qualification"]["BRA"]
        assert bra["prob_qualify"] == 0.0
        assert bra["status"] == "contention"

    def test_swing_by_country_grouped_and_sorted(self, client, session):
        _seed(session)
        body = client.get("/api/wc2026/summary").json()
        swings = body["swing_by_country"]
        # Scotland's two, biggest swing first.
        assert [m["match_id"] for m in swings["SCO"]] == ["m-sco-hai", "m-bra-mar"]
        assert swings["SCO"][0]["swing"] == 0.44
        assert swings["SCO"][0]["is_own_match"] is True
        # Brazil's own row is grouped separately under its own code.
        assert [m["match_id"] for m in swings["BRA"]] == ["m-bra-mar"]
        assert swings["BRA"][0]["is_own_match"] is True

    def test_cache_and_etag_headers(self, client, session):
        _seed(session)
        r = client.get("/api/wc2026/summary")
        assert r.status_code == 200
        assert r.headers["Cache-Control"] == "public, max-age=300"
        assert r.headers["ETag"].startswith('"')

    def test_conditional_get_returns_304(self, client, session):
        _seed(session)
        etag = client.get("/api/wc2026/summary").headers["ETag"]
        second = client.get("/api/wc2026/summary", headers={"If-None-Match": etag})
        assert second.status_code == 304
        assert second.headers["ETag"] == etag

    def test_no_standings_returns_503(self, client, session):
        # No standings rows at all: the page cannot render.
        r = client.get("/api/wc2026/summary")
        assert r.status_code == 503

    def test_qualification_default_when_sim_not_run(self, client, session):
        # Standings present but no qualification row yet: defaults, top-level n=0.
        session.add(
            _standing(
                "sco", "Scotland", "SCO", "C", pts=4, gf=3, ga=3, mp=3, w=1, d=1, l=1
            )
        )
        session.commit()
        body = client.get("/api/wc2026/summary").json()
        q = body["qualification"]["SCO"]
        assert q["status"] == "contention"
        assert q["prob_qualify"] == 0.0
        assert q["computed_at"] is None
        assert body["n_sims"] == 0
