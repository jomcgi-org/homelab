"""Scheduled job for the worldcup domain (WC2026 Scotland qualification tracker).

refresh_handler polls worldcup26.ir, upserts worldcup.standings and
worldcup.fixtures, then runs the Monte Carlo qualification simulation for the
focus team (Scotland) and persists worldcup.qualification (all teams) plus
worldcup.swing_matches (the focus team's decisive remaining fixtures).

The network phase runs in the async handler; every synchronous DB pass is
delegated to a worker thread (asyncio.to_thread) with its own Session so it
never blocks the scheduler event loop, mirroring dr_jobs.scrape_nhs_handler.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from worldcup import client, ratings, sim
from worldcup.models import Fixture, Qualification, Standing, SwingMatch

logger = logging.getLogger("monolith.worldcup")

# The focus team the simulation reports swings for: Scotland.
FOCUS_CODE = "SCO"


def _upsert(standings_rows: list[dict], fixture_rows: list[dict]) -> None:
    """Upsert standings (by team_id) and fixtures (by match_id) idempotently.

    Opens its own Session from get_engine() (this runs in a worker thread, off
    the scheduler event loop). session.merge() inserts a new row or replaces an
    existing one keyed on the primary key, so re-running over the same payload
    converges to the same state.
    """
    from sqlmodel import Session

    from app.db import get_engine

    now = datetime.now(timezone.utc)
    with Session(get_engine()) as session:
        for row in standings_rows:
            session.merge(Standing(**row, updated_at=now))
        for row in fixture_rows:
            session.merge(Fixture(**row, updated_at=now))
        session.commit()


def _build_sim_inputs(session) -> tuple[list[sim.TeamState], list[sim.Fixture]]:
    """Build simulation inputs from the persisted standings and fixtures.

    Every standings row becomes a TeamState. Only unfinished fixtures with both
    FIFA codes resolved become sim Fixtures (a fixture missing a code cannot be
    simulated and is skipped). A fixture involving the focus team is flagged
    is_own so the swing output can highlight Scotland's own matches.
    """
    from sqlmodel import select

    states = [
        sim.TeamState(
            team_id=s.team_id,
            fifa_code=s.fifa_code,
            group=s.group_name,
            pts=s.pts,
            gf=s.gf,
            ga=s.ga,
        )
        for s in session.exec(select(Standing)).all()
    ]

    fixtures: list[sim.Fixture] = []
    for f in session.exec(select(Fixture).where(Fixture.finished == False)).all():  # noqa: E712 - SQLAlchemy needs == not is
        if f.home_code is None or f.away_code is None:
            continue
        fixtures.append(
            sim.Fixture(
                match_id=f.match_id,
                group=f.group_name,
                home_code=f.home_code,
                away_code=f.away_code,
                is_own=FOCUS_CODE in (f.home_code, f.away_code),
            )
        )
    return states, fixtures


def _persist_sim(session, result: sim.SimResult, n: int) -> None:
    """Persist a SimResult into worldcup.qualification and worldcup.swing_matches.

    Qualification rows are upserted per team (keyed on team_id, resolved from
    standings). Swing rows are delete-then-insert: the table only ever holds the
    focus team's currently-remaining matches, so wiping and reinserting is the
    simplest way to guarantee no stale or duplicate rows survive a re-run.
    """
    from sqlmodel import select

    now = datetime.now(timezone.utc)

    standings = session.exec(select(Standing)).all()
    code_to_id = {s.fifa_code: s.team_id for s in standings}
    fixtures = session.exec(select(Fixture)).all()
    kickoff_by_match = {f.match_id: f.kickoff for f in fixtures}

    for prob in result.per_team.values():
        team_id = code_to_id.get(prob.fifa_code)
        if team_id is None:
            logger.warning(
                "worldcup: no team_id for %s in standings, skipping qualification",
                prob.fifa_code,
            )
            continue
        session.merge(
            Qualification(
                team_id=team_id,
                fifa_code=prob.fifa_code,
                prob_qualify=prob.prob_qualify,
                prob_top2=prob.prob_top2,
                prob_third=prob.prob_third,
                status=prob.status,
                n_sims=n,
                computed_at=now,
            )
        )

    focus_team_id = code_to_id.get(FOCUS_CODE)
    # Delete every existing swing row, then insert one per swing in this result.
    for existing in session.exec(select(SwingMatch)).all():
        session.delete(existing)
    if focus_team_id is None:
        logger.warning(
            "worldcup: focus team %s not in standings, skipping swing rows",
            FOCUS_CODE,
        )
    else:
        # add_all in one call (not session.add in a loop) so a partial failure
        # does not leave half the swing set committed.
        session.add_all(
            [
                SwingMatch(
                    match_id=swing.match_id,
                    focus_team_id=focus_team_id,
                    group_name=swing.group,
                    home_code=swing.home_code,
                    away_code=swing.away_code,
                    kickoff=kickoff_by_match.get(swing.match_id),
                    swing=swing.swing,
                    p_qualify_home_win=swing.p_qualify_home_win,
                    p_qualify_draw=swing.p_qualify_draw,
                    p_qualify_away_win=swing.p_qualify_away_win,
                    is_own_match=swing.is_own_match,
                    computed_at=now,
                )
                for swing in result.swings
            ]
        )
    session.commit()


def _simulate_and_store() -> None:
    """Read sim inputs from the DB, run the Monte Carlo, persist the result.

    Opens its own Session (runs in a worker thread). If any unfinished fixture
    references a FIFA code missing from the frozen Elo snapshot we log a warning
    and return WITHOUT simulating, rather than defaulting a rating: a missing
    rating is a data problem to surface loudly, not silently paper over, and it
    must not crash the scheduler.
    """
    from sqlmodel import Session

    from app.db import get_engine

    with Session(get_engine()) as session:
        states, fixtures = _build_sim_inputs(session)
        elo = ratings.load_elo()

        needed = {f.home_code for f in fixtures} | {f.away_code for f in fixtures}
        missing = sorted(c for c in needed if c not in elo)
        if missing:
            logger.warning(
                "worldcup: fixtures reference codes missing from Elo snapshot %s; "
                "skipping simulation",
                missing,
            )
            return

        result = sim.simulate(
            states,
            fixtures,
            elo,
            focus=FOCUS_CODE,
            n=int(os.environ.get("WORLDCUP_SIM_N", "20000")),
            seed=None,
        )
        _persist_sim(session, result, result.n)


async def refresh_handler(session) -> datetime | None:
    """Scheduled: poll worldcup26.ir, upsert standings+fixtures, simulate, persist.

    The scheduler passes a Session but the DB work uses its own sessions inside
    the worker threads. Returning None lets the scheduler compute the next run
    from the configured interval.
    """
    import httpx

    base = os.environ.get("WORLDCUP_API_BASE", "https://worldcup26.ir")
    async with httpx.AsyncClient(base_url=base, timeout=20) as http:
        standings_rows, fixture_rows, stats = await client.fetch_all(http)

    logger.info(
        "worldcup refresh: fetched %d standings, %d fixtures (%d finished) errors=%s",
        stats.get("standings", 0),
        stats.get("fixtures", 0),
        stats.get("finished", 0),
        stats.get("errors", []),
    )

    await asyncio.to_thread(_upsert, standings_rows, fixture_rows)
    await asyncio.to_thread(_simulate_and_store)
    return None
