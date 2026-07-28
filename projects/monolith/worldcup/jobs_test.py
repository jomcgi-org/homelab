"""Unit tests for worldcup.jobs pure DB adapters.

The worldcup SQLModel tables are schema-qualified (schema="worldcup"), which
SQLite has no concept of. We strip the schema off every table for the duration
of the test and recreate them on an in-memory SQLite engine, mirroring exactly
the fixture in dr_jobs/jobs_test.py (engine_fixture). core.db.get_engine is
monkeypatched at the test engine so any adapter that opens its own Session lands
on it too.

These tests exercise the pure adapters (_build_sim_inputs, _persist_sim) and
never touch the network: refresh_handler's httpx call is not exercised here.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import core.db as app_db
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


def _fixture(
    match_id,
    home,
    away,
    *,
    finished,
    group="A",
    kickoff=None,
    matchday=3,
    home_score=None,
    away_score=None,
):
    return Fixture(
        match_id=match_id,
        group_name=group,
        matchday=matchday,
        home_id=f"id-{home}",
        home_name=home,
        home_code=home,
        away_id=f"id-{away}",
        away_name=away,
        away_code=away,
        finished=finished,
        kickoff=kickoff,
        home_score=home_score,
        away_score=away_score,
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

    # Note: fixtures with an unresolved home_code/away_code cannot reach the DB
    # (the columns are NOT NULL); they are filtered upstream at parse time in
    # client.parse_fixtures, covered by client_test, so there is nothing to
    # exercise here at the _build_sim_inputs layer.


class TestBuildFinishedGames:
    def test_returns_finished_games_with_scores_only(self, engine):
        with Session(engine) as session:
            # Finished with scores -> usable as likelihood.
            session.add(
                _fixture(
                    "F-GER-FRA",
                    "GER",
                    "FRA",
                    finished=True,
                    matchday=1,
                    home_score=2,
                    away_score=1,
                )
            )
            # Unfinished -> excluded (no result yet).
            session.add(_fixture("F-SCO-GER", "SCO", "GER", finished=False))
            # Marked finished but scores absent -> excluded defensively.
            session.add(_fixture("F-WAL-NIR", "WAL", "NIR", finished=True, matchday=2))
            session.commit()

            games = jobs._build_finished_games(session)

        assert [g.home_code for g in games] == ["GER"]
        assert games[0].away_code == "FRA"
        assert (games[0].home_score, games[0].away_score) == (2, 1)
        assert games[0].matchday == 1


def _sim_result():
    """A small hand-built SimResult: SCO and GER in contention (each with swings)
    plus an eliminated HAI whose swing rows must NOT be persisted."""
    per_team = {
        "SCO": TeamProb("SCO", 0.62, 0.40, 0.22, "contention"),
        "GER": TeamProb("GER", 0.55, 0.45, 0.10, "contention"),
        "HAI": TeamProb("HAI", 0.0, 0.0, 0.0, "eliminated"),
    }
    swings_by_country = {
        "SCO": [
            Swing("F-SCO-GER", "A", "SCO", "GER", 0.5, 0.9, 0.6, 0.4, True),
            Swing("F-FRA-AND", "A", "FRA", "AND", 0.1, 0.65, 0.62, 0.6, False),
        ],
        "GER": [
            Swing("F-SCO-GER", "A", "SCO", "GER", 0.4, 0.3, 0.5, 0.85, True),
        ],
        # Eliminated team: present in the result but must be filtered out.
        "HAI": [
            Swing("F-SCO-GER", "A", "SCO", "GER", 0.0, 0.0, 0.0, 0.0, False),
        ],
    }
    return SimResult(
        per_team=per_team,
        swings=swings_by_country["SCO"],
        n=5000,
        swings_by_country=swings_by_country,
    )


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
            # SCO's two + GER's one; eliminated HAI's row is filtered out.
            assert len(swings) == 3
            assert session.get(SwingMatch, ("F-SCO-GER", "HAI")) is None
            # Composite key is (match_id, country_code).
            own = session.get(SwingMatch, ("F-SCO-GER", "SCO"))
            assert own.country_code == "SCO"
            assert own.is_own_match is True
            # SQLite drops tzinfo (Postgres preserves it); normalise to UTC,
            # mirroring the router's _as_utc serialisation, before comparing.
            assert own.kickoff.replace(tzinfo=timezone.utc) == kickoff

    def test_rerun_replaces_swing_rows_without_duplicates(self, engine):
        with Session(engine) as session:
            session.add_all([_standing("SCO"), _standing("GER")])
            session.add(_fixture("F-SCO-GER", "SCO", "GER", finished=False))
            session.commit()

            jobs._persist_sim(session, _sim_result(), 5000)
            # Second run with a single country/swing must wipe the first run's rows.
            single_swing = Swing(
                "F-SCO-GER", "A", "SCO", "GER", 0.5, 0.9, 0.6, 0.4, True
            )
            single = SimResult(
                per_team={"SCO": TeamProb("SCO", 0.7, 0.5, 0.2, "contention")},
                swings=[single_swing],
                n=6000,
                swings_by_country={"SCO": [single_swing]},
            )
            jobs._persist_sim(session, single, 6000)

        with Session(engine) as session:
            swings = session.exec(select(SwingMatch)).all()
            assert len(swings) == 1  # no duplicates; prior SCO/GER/HAI rows gone
            assert swings[0].match_id == "F-SCO-GER"
            assert swings[0].country_code == "SCO"
            assert session.get(Qualification, "id-SCO").n_sims == 6000

    def test_sub_threshold_swings_are_dropped(self, engine):
        """A contending team's matches whose swing is below the minimum (every
        outcome within a percentage point) must not be stored as dead cards."""
        with Session(engine) as session:
            session.add_all([_standing("SCO"), _standing("GER")])
            session.add(_fixture("F-SCO-GER", "SCO", "GER", finished=False))
            session.add(_fixture("F-FRA-AND", "FRA", "AND", finished=False))
            session.commit()

            real = Swing("F-SCO-GER", "A", "SCO", "GER", 0.5, 0.9, 0.6, 0.4, True)
            # 0.005 spread: all three outcomes within a percentage point.
            flat = Swing(
                "F-FRA-AND", "A", "FRA", "AND", 0.005, 0.62, 0.62, 0.615, False
            )
            result = SimResult(
                per_team={"SCO": TeamProb("SCO", 0.62, 0.40, 0.22, "contention")},
                swings=[real, flat],
                n=5000,
                swings_by_country={"SCO": [real, flat]},
            )
            jobs._persist_sim(session, result, 5000)

        with Session(engine) as session:
            swings = session.exec(select(SwingMatch)).all()
            assert len(swings) == 1
            assert swings[0].match_id == "F-SCO-GER"
            assert session.get(SwingMatch, ("F-FRA-AND", "SCO")) is None


def _standing_row(code, *, group="A", pts=3, gf=3, ga=2):
    return {
        "team_id": f"id-{code}",
        "name": code,
        "fifa_code": code,
        "flag_url": None,
        "group_name": group,
        "mp": 2,
        "w": 1,
        "d": 0,
        "l": 1,
        "pts": pts,
        "gf": gf,
        "ga": ga,
        "gd": gf - ga,
    }


def _fixture_row(
    match_id, home, away, *, finished, group="A", home_score=None, away_score=None
):
    return {
        "match_id": match_id,
        "group_name": group,
        "matchday": 3,
        "home_id": f"id-{home}",
        "home_name": home,
        "home_code": home,
        "away_id": f"id-{away}",
        "away_name": away,
        "away_code": away,
        "home_score": home_score,
        "away_score": away_score,
        "finished": finished,
        "kickoff": None,
    }


class TestUpsertChangeDetection:
    def test_first_insert_is_sim_changed(self, engine):
        changed = jobs._upsert(
            [_standing_row("SCO")], [_fixture_row("F1", "SCO", "GER", finished=False)]
        )
        assert changed is True

    def test_identical_rerun_not_changed(self, engine):
        s = [_standing_row("SCO")]
        f = [_fixture_row("F1", "SCO", "GER", finished=False)]
        jobs._upsert(s, f)
        assert jobs._upsert(s, f) is False

    def test_points_change_is_sim_changed(self, engine):
        jobs._upsert([_standing_row("SCO", pts=3)], [])
        assert jobs._upsert([_standing_row("SCO", pts=6, gf=4)], []) is True

    def test_fixture_finished_flip_is_sim_changed(self, engine):
        jobs._upsert([], [_fixture_row("F1", "SCO", "GER", finished=False)])
        flipped = _fixture_row(
            "F1", "SCO", "GER", finished=True, home_score=1, away_score=0
        )
        assert jobs._upsert([], [flipped]) is True

    def test_live_score_on_unfinished_is_not_sim_changed(self, engine):
        # A score ticking up on an in-progress (unfinished) match updates the row
        # for display but must NOT re-trigger the expensive simulation: an
        # unfinished score does not feed the Elo posterior.
        jobs._upsert([], [_fixture_row("F1", "SCO", "GER", finished=False)])
        live = _fixture_row(
            "F1", "SCO", "GER", finished=False, home_score=1, away_score=0
        )
        assert jobs._upsert([], [live]) is False


def _fake_result():
    return SimResult(
        per_team={"SCO": TeamProb("SCO", 0.8, 0.1, 0.7, "contention")},
        swings=[],
        n=0,
    )


class TestSimulateGate:
    def _seed(self, session, *, qual_n, with_swing=True):
        session.add_all([_standing("SCO"), _standing("GER", pts=4)])
        session.add(_fixture("F-SCO-GER", "SCO", "GER", finished=False))
        if with_swing:
            # A populated swing table; without one the self-heal gate would fire.
            session.add(
                SwingMatch(
                    match_id="F-SCO-GER",
                    country_code="SCO",
                    group_name="A",
                    home_code="SCO",
                    away_code="GER",
                    swing=0.3,
                    p_qualify_home_win=0.9,
                    p_qualify_draw=0.6,
                    p_qualify_away_win=0.4,
                    is_own_match=True,
                )
            )
        session.add(
            Qualification(
                team_id="id-SCO",
                fifa_code="SCO",
                prob_qualify=0.8,
                prob_top2=0.1,
                prob_third=0.7,
                status="contention",
                n_sims=qual_n,
            )
        )
        session.commit()

    def test_skips_when_unchanged_and_n_matches(self, engine, monkeypatch):
        current_n = int(os.environ.get("WORLDCUP_SIM_N", "500000"))
        with Session(engine) as session:
            self._seed(session, qual_n=current_n)
        calls = []
        monkeypatch.setattr(jobs.sim, "simulate", lambda *a, **k: calls.append(1))
        jobs._simulate_and_store(inputs_changed=False)
        assert calls == []  # simulation skipped

    def test_runs_when_inputs_changed(self, engine, monkeypatch):
        current_n = int(os.environ.get("WORLDCUP_SIM_N", "500000"))
        with Session(engine) as session:
            self._seed(session, qual_n=current_n)
        calls = []
        monkeypatch.setattr(
            jobs.sim, "simulate", lambda *a, **k: (calls.append(1), _fake_result())[1]
        )
        jobs._simulate_and_store(inputs_changed=True)
        assert len(calls) == 1

    def test_runs_when_n_changed_even_if_inputs_same(self, engine, monkeypatch):
        # Stored qualification used a different N (a deploy bumped WORLDCUP_SIM_N):
        # recompute once even though no match result moved.
        with Session(engine) as session:
            self._seed(session, qual_n=123)
        calls = []
        monkeypatch.setattr(
            jobs.sim, "simulate", lambda *a, **k: (calls.append(1), _fake_result())[1]
        )
        jobs._simulate_and_store(inputs_changed=False)
        assert len(calls) == 1

    def test_runs_when_swing_missing_even_if_inputs_same(self, engine, monkeypatch):
        # Remaining fixtures exist but the swing table is empty (e.g. just
        # recreated by a migration): self-heal by recomputing, even when inputs
        # and N are unchanged.
        current_n = int(os.environ.get("WORLDCUP_SIM_N", "500000"))
        with Session(engine) as session:
            self._seed(session, qual_n=current_n, with_swing=False)
        calls = []
        monkeypatch.setattr(
            jobs.sim, "simulate", lambda *a, **k: (calls.append(1), _fake_result())[1]
        )
        jobs._simulate_and_store(inputs_changed=False)
        assert len(calls) == 1
