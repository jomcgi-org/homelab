"""Unit tests for worldcup.jobs pure DB adapters.

The worldcup SQLModel tables are schema-qualified (schema="worldcup"), which
SQLite has no concept of. We strip the schema off every table for the duration
of the test and recreate them on an in-memory SQLite engine, mirroring exactly
the fixture in dr_jobs/jobs_test.py (engine_fixture). app.db.get_engine is
monkeypatched at the test engine so any adapter that opens its own Session lands
on it too.

These tests exercise the pure adapters (_build_sim_inputs, _persist_sim) and
never touch the network: refresh_handler's httpx call is not exercised here.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import app.db as app_db
import worldcup.jobs as jobs
from worldcup.models import Fixture, Qualification, Standing, SwingMatch
from worldcup.sim import SimResult, Swing, TeamProb


@pytest.fixture(name="engine")
def engine_fixture(monkeypatch):
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
        # Adapters that open Session(get_engine()) must hit the test engine.
        monkeypatch.setattr(app_db, "get_engine", lambda: engine)
        yield engine
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


def _standing(code, group="A", pts=3, gf=3, ga=2):
    return Standing(
        team_id=f"id-{code}",
        name=code,
        fifa_code=code,
        group_name=group,
        pts=pts,
        gf=gf,
        ga=ga,
    )


def _fixture(match_id, home, away, *, finished, group="A", kickoff=None):
    return Fixture(
        match_id=match_id,
        group_name=group,
        matchday=3,
        home_id=f"id-{home}",
        home_name=home,
        home_code=home,
        away_id=f"id-{away}",
        away_name=away,
        away_code=away,
        finished=finished,
        kickoff=kickoff,
    )


class TestBuildSimInputs:
    def test_returns_only_unfinished_fixtures_and_flags_own(self, engine):
        with Session(engine) as session:
            session.add_all(
                [
                    _standing("SCO"),
                    _standing("GER"),
                    _standing("FRA", pts=6),
                    _standing("AND", pts=0),
                ]
            )
            session.add(_fixture("F-GER-FRA", "GER", "FRA", finished=True))
            session.add(_fixture("F-SCO-GER", "SCO", "GER", finished=False))
            session.commit()

            states, fixtures = jobs._build_sim_inputs(session)

        assert len(states) == 4  # one TeamState per standings row
        assert {s.fifa_code for s in states} == {"SCO", "GER", "FRA", "AND"}
        # Only the unfinished fixture survives.
        assert [f.match_id for f in fixtures] == ["F-SCO-GER"]
        assert fixtures[0].is_own is True  # involves the focus team SCO

    def test_skips_fixtures_with_missing_codes(self, engine):
        with Session(engine) as session:
            session.add_all([_standing("SCO"), _standing("GER")])
            bad = _fixture("F-bad", "SCO", "GER", finished=False)
            bad.away_code = None
            session.add(bad)
            session.add(_fixture("F-ok", "SCO", "GER", finished=False))
            session.commit()

            _, fixtures = jobs._build_sim_inputs(session)

        assert [f.match_id for f in fixtures] == ["F-ok"]


def _sim_result():
    """A small hand-built SimResult: SCO qualifying with two swing matches."""
    per_team = {
        "SCO": TeamProb(
            "SCO",
            prob_qualify=0.62,
            prob_top2=0.40,
            prob_third=0.22,
            status="contention",
        ),
        "GER": TeamProb(
            "GER",
            prob_qualify=0.55,
            prob_top2=0.45,
            prob_third=0.10,
            status="contention",
        ),
    }
    swings = [
        Swing(
            "F-SCO-GER",
            "A",
            "SCO",
            "GER",
            swing=0.5,
            p_qualify_home_win=0.9,
            p_qualify_draw=0.6,
            p_qualify_away_win=0.4,
            is_own_match=True,
        ),
        Swing(
            "F-FRA-AND",
            "A",
            "FRA",
            "AND",
            swing=0.1,
            p_qualify_home_win=0.65,
            p_qualify_draw=0.62,
            p_qualify_away_win=0.6,
            is_own_match=False,
        ),
    ]
    return SimResult(per_team=per_team, swings=swings, n=5000)


class TestPersistSim:
    def test_persists_qualification_and_swings(self, engine):
        kickoff = datetime(2026, 6, 25, 18, 0, tzinfo=timezone.utc)
        with Session(engine) as session:
            session.add_all([_standing("SCO"), _standing("GER")])
            session.add(
                _fixture("F-SCO-GER", "SCO", "GER", finished=False, kickoff=kickoff)
            )
            session.commit()

            jobs._persist_sim(session, _sim_result(), 5000)

        with Session(engine) as session:
            sco = session.get(Qualification, "id-SCO")
            assert sco is not None
            assert sco.fifa_code == "SCO"
            assert sco.prob_qualify == 0.62
            assert sco.prob_top2 == 0.40
            assert sco.prob_third == 0.22
            assert sco.n_sims == 5000

            swings = session.exec(select(SwingMatch)).all()
            assert len(swings) == 2  # one row per swing
            own = session.get(SwingMatch, "F-SCO-GER")
            assert own.focus_team_id == "id-SCO"
            assert own.is_own_match is True
            assert own.kickoff == kickoff  # joined from the fixture row

    def test_rerun_replaces_swing_rows_without_duplicates(self, engine):
        with Session(engine) as session:
            session.add_all([_standing("SCO"), _standing("GER")])
            session.add(_fixture("F-SCO-GER", "SCO", "GER", finished=False))
            session.commit()

            jobs._persist_sim(session, _sim_result(), 5000)
            # Second run with a single swing must wipe the first run's rows.
            single = SimResult(
                per_team={
                    "SCO": TeamProb("SCO", 0.7, 0.5, 0.2, "contention"),
                },
                swings=[
                    Swing("F-SCO-GER", "A", "SCO", "GER", 0.5, 0.9, 0.6, 0.4, True),
                ],
                n=6000,
            )
            jobs._persist_sim(session, single, 6000)

        with Session(engine) as session:
            swings = session.exec(select(SwingMatch)).all()
            assert len(swings) == 1  # no duplicates, F-FRA-AND was removed
            assert swings[0].match_id == "F-SCO-GER"
            assert session.get(Qualification, "id-SCO").n_sims == 6000
