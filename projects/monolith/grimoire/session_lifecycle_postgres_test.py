"""Session starts and terminal transitions against real PostgreSQL migrations."""

from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, text
from sqlalchemy.pool import NullPool
from sqlmodel import Session, create_engine, select

from grimoire.models import (
    AppUser,
    Campaign,
    CampaignMember,
    GameSession,
    PlayerCharacter,
)
from grimoire.router import (
    GameSessionUpdateRequest,
    create_game_session,
    update_game_session,
)


@pytest.fixture
def lane(pg):
    engine = create_engine(
        pg.url,
        poolclass=NullPool,
        connect_args={"options": "-c lock_timeout=10000 -c statement_timeout=15000"},
    )
    identity = uuid4().hex
    with Session(engine) as session:
        dm = AppUser(email=f"session-dm-{identity}@example.test")
        campaigns = [Campaign(name=f"Session {identity} {index}") for index in range(2)]
        session.add_all([dm, *campaigns])
        session.flush()
        session.add_all(
            CampaignMember(campaign_id=campaign.id, app_user_id=dm.id, role="dm")
            for campaign in campaigns
        )
        session.commit()
        info = SimpleNamespace(
            engine=engine,
            campaign_id=campaigns[0].id,
            other_campaign_id=campaigns[1].id,
            dm_email=dm.email,
        )
    try:
        yield info
    finally:
        engine.dispose()


def _start(lane, *, label="", pids=None, campaign_id=None):
    with Session(lane.engine) as session:
        connection = session.connection()
        connection.info["lifecycle_worker"] = label
        if pids is not None:
            pids.put(connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
        try:
            row = create_game_session(
                campaign_id or lane.campaign_id, lane.dm_email, session
            )
            return 200, row.id
        except HTTPException as error:
            return error.status_code, error.detail


def _wait_for_block_or_crossing(engine, pid, crossed):
    """Observe the competitor reaching a PostgreSQL lock or passing the guard."""
    deadline = monotonic() + 5
    with engine.connect() as observer:
        while monotonic() < deadline:
            if crossed.is_set():
                return False
            if observer.execute(
                text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}
            ).scalar_one():
                return True
            sleep(0.01)
    pytest.fail("competing connection neither blocked nor crossed the guard")


def test_concurrent_starts_return_one_session_and_one_conflict(lane):
    counted, release, crossed = Event(), Event(), Event()
    pids = Queue()

    def after_active_read(connection, _cursor, statement, _parameters, _context, _many):
        if (
            "FROM grimoire.game_session" not in statement
            or "status !=" not in statement
        ):
            return
        worker = connection.info.get("lifecycle_worker")
        if worker == "first" and not counted.is_set():
            counted.set()
            assert release.wait(10), "first active-session read was never released"
        elif worker == "second":
            crossed.set()

    event.listen(lane.engine, "after_cursor_execute", after_active_read)
    try:
        with ThreadPoolExecutor(max_workers=3) as workers:
            first = workers.submit(_start, lane, label="first", pids=pids)
            first_pid = pids.get(timeout=5)
            try:
                assert counted.wait(5), "first start did not check active sessions"
                second = workers.submit(_start, lane, label="second", pids=pids)
                second_pid = pids.get(timeout=5)
                assert first_pid != second_pid
                blocked = _wait_for_block_or_crossing(lane.engine, second_pid, crossed)
                other = workers.submit(_start, lane, campaign_id=lane.other_campaign_id)
                assert other.result(timeout=5)[0] == 200
                assert blocked, "second start read before the first start committed"
            finally:
                release.set()
            outcomes = [first.result(timeout=5), second.result(timeout=5)]
            assert [outcome[0] for outcome in outcomes] == [200, 409]
            assert (
                outcomes[1][1]
                == "an active or paused session already exists for this campaign"
            )
    finally:
        release.set()
        event.remove(lane.engine, "after_cursor_execute", after_active_read)
    with Session(lane.engine) as session:
        rows = session.exec(
            select(GameSession).where(GameSession.campaign_id == lane.campaign_id)
        ).all()
        assert len(rows) == 1
        assert rows[0].id == outcomes[0][1]
        assert rows[0].status == "active"


@pytest.mark.parametrize("status", ["active", "paused"])
def test_end_wins_over_waiting_transition_with_cached_session(lane, status):
    _, session_id = _start(lane)
    cached, end_written, release, crossed = Event(), Event(), Event(), Event()
    pids = Queue()

    def hold_end(connection, _cursor, statement, _parameters, _context, _many):
        if (
            connection.info.get("lifecycle_worker") == "end"
            and statement.startswith("UPDATE grimoire.game_session")
            and not end_written.is_set()
        ):
            end_written.set()
            assert release.wait(10), "end transition was never released"

    def change(next_status, label):
        with Session(lane.engine) as session:
            connection = session.connection()
            connection.info["lifecycle_worker"] = label
            if label == "stale":
                old = session.get(GameSession, session_id)
                assert old.status == "active"
                cached.set()
                assert end_written.wait(5), "end did not write its transition"
                pids.put(
                    connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                )
            try:
                row = update_game_session(
                    lane.campaign_id,
                    session_id,
                    GameSessionUpdateRequest(status=next_status),
                    lane.dm_email,
                    session,
                )
                return 200, row.ended_at
            except HTTPException as error:
                return error.status_code, error.detail
            finally:
                if label == "stale":
                    crossed.set()

    event.listen(lane.engine, "after_cursor_execute", hold_end)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            stale = workers.submit(change, status, "stale")
            try:
                assert cached.wait(5)
                ending = workers.submit(change, "ended", "end")
                pid = pids.get(timeout=5)
                assert _wait_for_block_or_crossing(lane.engine, pid, crossed)
            finally:
                release.set()
            first = ending.result(timeout=5)
            assert first[0] == 200
            assert stale.result(timeout=5) == (409, "ended sessions cannot be reopened")
    finally:
        release.set()
        event.remove(lane.engine, "after_cursor_execute", hold_end)
    with Session(lane.engine) as session:
        row = session.get(GameSession, session_id)
        assert row.status == "ended"
        assert row.ended_at == first[1]


def _add_player(lane):
    with Session(lane.engine) as session:
        player = AppUser(email=f"session-player-{uuid4().hex}@example.test")
        character = PlayerCharacter(
            campaign_id=lane.campaign_id, character_name="New seat"
        )
        session.add_all([player, character])
        session.flush()
        session.add(
            CampaignMember(
                campaign_id=lane.campaign_id,
                app_user_id=player.id,
                role="player",
                player_character_id=character.id,
            )
        )
        session.commit()


def _jobs(lane, session_id):
    with lane.engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT name, payload, next_run_at FROM claude_agent.routine_jobs WHERE name LIKE :prefix ORDER BY name"
            ),
            {"prefix": f"grimoire-kg-drain:{session_id}:%"},
        ).all()


def test_repeated_end_keeps_original_fact_queue_after_seating_changes(lane):
    _add_player(lane)
    _, session_id = _start(lane)
    with Session(lane.engine) as session:
        ended = update_game_session(
            lane.campaign_id,
            session_id,
            GameSessionUpdateRequest(status="ended"),
            lane.dm_email,
            session,
        )
        first_end = ended.ended_at
    before = _jobs(lane, session_id)
    assert len(before) == 2
    _add_player(lane)
    with Session(lane.engine) as session:
        repeated = update_game_session(
            lane.campaign_id,
            session_id,
            GameSessionUpdateRequest(status="ended"),
            lane.dm_email,
            session,
        )
        assert repeated.ended_at == first_end
    assert _jobs(lane, session_id) == before
