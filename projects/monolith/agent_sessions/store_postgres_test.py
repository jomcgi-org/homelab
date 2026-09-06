"""PostgreSQL transaction coverage for unknown invocation outcomes.

Run through the registered BDD target. These tests use the harness's migrated
database and independent committed connections, never its SAVEPOINT fixture.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from queue import Queue
from threading import Event, local
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool
from sqlmodel import Session, create_engine

from agent_sessions import store
from agent_sessions.models import AgentSession, AgentTurn, PendingMessage
from agent_sessions.transport import Turn

WAIT_SECONDS = 5
HOLDER_WAIT_SECONDS = 15
OWNER = "original-owner"


@pytest.fixture
def lane(pg, monkeypatch):
    identity = uuid4().hex[:12]
    context = local()
    engines = {}

    def engine_for(actor):
        if actor not in engines:
            engines[actor] = create_engine(
                pg.url,
                poolclass=NullPool,
                connect_args={
                    "application_name": f"unknown-{identity}-{actor}",
                    "connect_timeout": WAIT_SECONDS,
                    "options": "-c lock_timeout=15000 -c statement_timeout=20000",
                },
            )
        return engines[actor]

    engine = engine_for("observer")
    monkeypatch.setattr(store, "get_engine", lambda: getattr(context, "engine", engine))

    def run(actor, operation):
        context.actor = actor
        context.engine = engine_for(actor)
        return operation()

    with Session(engine) as session:
        row = store.create_session(session, f"postgres-{identity}", "<guest>", "main")
        session_id = row.id
        token = row.progress_token
        row.ember_session_id = "guest-retain"
        row.ember_lineage_id = "lineage-retain"
        session.add(row)
        session.commit()
        store.create_pending_message(session, session_id, "original prompt", "terra")
        store.create_pending_message(
            session, session_id, "untouched successor", "terra"
        )
    assert store.claim_pending_message_for_session_sync(session_id, OWNER) == 1
    assert (
        store.write_progress_sync(token, "partial result", [{"tool": "read"}]) == "ok"
    )
    try:
        yield SimpleNamespace(
            engine=engine,
            context=context,
            session_id=session_id,
            token=token,
            run=run,
            application_name=lambda actor: f"unknown-{identity}-{actor}",
        )
    finally:
        with engine.begin() as connection:
            connection.execute(
                delete(PendingMessage).where(PendingMessage.session_id == session_id)
            )
            connection.execute(
                delete(AgentTurn).where(AgentTurn.session_id == session_id)
            )
            connection.execute(
                delete(AgentSession).where(AgentSession.id == session_id)
            )
        for actor_engine in engines.values():
            actor_engine.dispose()


def _pause_after_session_lock(lane, monkeypatch, actor):
    """Pause only after production SQL acquired the winner's real row lock."""
    locked = Queue(maxsize=1)
    proceed = Event()
    original = store._lock_session

    def lock(session, session_id):
        row = original(session, session_id)
        if getattr(lane.context, "actor", None) == actor and not proceed.is_set():
            locked.put(
                session.execute(text("SELECT pg_backend_pid()")).scalar_one(),
                timeout=WAIT_SECONDS,
            )
            assert proceed.wait(HOLDER_WAIT_SECONDS), "row-lock holder was not released"
        return row

    monkeypatch.setattr(store, "_lock_session", lock)
    return locked, proceed


def _wait_for_blocked_connection(lane, actor, blocker_pid):
    """Verify lock contention in PostgreSQL instead of inferring it from timing."""
    deadline = monotonic() + WAIT_SECONDS
    poll = Event()
    while monotonic() < deadline:
        with lane.engine.connect() as connection:
            blocked_pid = connection.execute(
                text("""
                    SELECT pid FROM pg_stat_activity
                     WHERE application_name = :actor
                       AND wait_event_type = 'Lock'
                       AND :blocker = ANY(pg_blocking_pids(pid))
                """),
                {"actor": lane.application_name(actor), "blocker": blocker_pid},
            ).scalar_one_or_none()
        if blocked_pid is not None:
            assert blocked_pid != blocker_pid
            return
        # Poll the observed lock condition. This delay does not order the race.
        poll.wait(0.01)
    pytest.fail(f"{actor} did not wait on PostgreSQL backend {blocker_pid}")


def _hold(lane):
    return store.finish_unknown_pending_sync(
        lane.session_id, 1, OWNER, 1, "observer_lost"
    )


def _complete(lane):
    turn = Turn(
        result="completed result",
        terminal_reason="completed",
        stop_reason="end_turn",
        is_error=False,
        permission_denials=[],
        num_turns=1,
        session_id="cli-done",
        usage={},
        total_cost_usd=0.01,
        duration_ms=1,
        activities=[],
    )
    try:
        store.persist_turn_from_pending_sync(
            lane.session_id,
            1,
            "original prompt",
            turn,
            "done",
            "completed",
            "cli-done",
            "terra",
            OWNER,
            1,
        )
        return "completed"
    except store.SessionOutcomeUnknown:
        return "held"


def test_heartbeat_commits_before_waiting_reclaim_rechecks_staleness(lane, monkeypatch):
    with lane.engine.begin() as connection:
        connection.execute(
            text("""
                UPDATE agent_sessions.pending_messages
                   SET claimed_at = now() - interval '2 minutes'
                 WHERE session_id = :session_id AND seq = 1
            """),
            {"session_id": lane.session_id},
        )
    locked, proceed = _pause_after_session_lock(lane, monkeypatch, "heartbeat")
    with ThreadPoolExecutor(max_workers=2) as pool:
        heartbeat = pool.submit(
            lane.run,
            "heartbeat",
            lambda: store.refresh_claim_sync(lane.session_id, 1, OWNER),
        )
        try:
            blocker_pid = locked.get(timeout=WAIT_SECONDS)
            reclaim = pool.submit(lane.run, "reclaim", store.reclaim_stale_claims_sync)
            _wait_for_blocked_connection(lane, "reclaim", blocker_pid)
        finally:
            proceed.set()
        assert heartbeat.result(timeout=WAIT_SECONDS) is True
        assert reclaim.result(timeout=WAIT_SECONDS) == 0
    with Session(lane.engine) as session:
        pending = store.get_pending_message(session, lane.session_id, 1)
        assert pending.claimed_by_replica == OWNER
        assert pending.dispatch_count == 1
        assert store.get_turn(session, lane.session_id, 1) is None
        assert store.get_session(session, lane.session_id).status == "running"


def test_activation_waits_for_unknown_commit_and_keeps_session_held(lane, monkeypatch):
    locked, proceed = _pause_after_session_lock(lane, monkeypatch, "hold")

    def activate():
        with Session(store.get_engine()) as session:
            return store.activate_session_after_enqueue(session, lane.session_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        hold = pool.submit(lane.run, "hold", lambda: _hold(lane))
        try:
            blocker_pid = locked.get(timeout=WAIT_SECONDS)
            activation = pool.submit(lane.run, "activation", activate)
            _wait_for_blocked_connection(lane, "activation", blocker_pid)
        finally:
            proceed.set()
        assert hold.result(timeout=WAIT_SECONDS) is True
        assert activation.result(timeout=WAIT_SECONDS) is False
    with Session(lane.engine) as session:
        assert store.get_session(session, lane.session_id).status == "failed"
        assert (
            store.get_turn(session, lane.session_id, 1).stop_reason
            == store.UNKNOWN_INVOCATION
        )
        assert (
            store.get_pending_message(session, lane.session_id, 2).dispatch_count == 0
        )


@pytest.mark.parametrize("winner", ["completion", "hold"])
def test_completion_and_reconciliation_commit_exactly_one_outcome(
    lane, monkeypatch, winner
):
    operations = {"completion": lambda: _complete(lane), "hold": lambda: _hold(lane)}
    loser = "hold" if winner == "completion" else "completion"
    locked, proceed = _pause_after_session_lock(lane, monkeypatch, winner)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(lane.run, winner, operations[winner])
        try:
            blocker_pid = locked.get(timeout=WAIT_SECONDS)
            second = pool.submit(lane.run, loser, operations[loser])
            _wait_for_blocked_connection(lane, loser, blocker_pid)
        finally:
            proceed.set()
        results = {
            winner: first.result(timeout=WAIT_SECONDS),
            loser: second.result(timeout=WAIT_SECONDS),
        }
    held = winner == "hold"
    assert results == {"completion": "held" if held else "completed", "hold": held}
    with Session(lane.engine) as session:
        turns = store.get_turns(session, lane.session_id)
        assert len(turns) == 1
        assert turns[0].prompt == "original prompt"
        assert turns[0].result_text == (
            "partial result" if held else "completed result"
        )
        assert turns[0].stop_reason == (
            store.UNKNOWN_INVOCATION if held else "end_turn"
        )
        assert turns[0].cost_usd == (None if held else 0.01)
        row = store.get_session(session, lane.session_id)
        assert row.status == ("failed" if held else "completed")
        assert row.progress_token == (None if held else lane.token)
        assert row.ember_session_id == "guest-retain"
        assert row.ember_lineage_id == "lineage-retain"
        assert store.get_pending_message(session, lane.session_id, 1) is None
        assert (
            store.get_pending_message(session, lane.session_id, 2).dispatch_count == 0
        )
        if held:
            assert json.loads(turns[0].usage_json)["recovery"]["dispatch_count"] == 1


def test_pending_delete_failure_rolls_back_unknown_record_and_session_hold(lane):
    # Inject a server-side failure in the same transaction as the outcome
    # record and pending disposition. Remove the trigger before fixture cleanup.
    name = f"reject_unknown_{lane.session_id}"
    with lane.engine.begin() as connection:
        connection.execute(
            text(f"""
            CREATE FUNCTION agent_sessions.{name}() RETURNS trigger AS $$
            BEGIN
                IF OLD.session_id = {lane.session_id} THEN
                    RAISE EXCEPTION 'injected pending disposition failure';
                END IF;
                RETURN OLD;
            END;
            $$ LANGUAGE plpgsql
        """)
        )
        connection.execute(
            text(f"""
            CREATE TRIGGER {name} BEFORE DELETE ON agent_sessions.pending_messages
            FOR EACH ROW EXECUTE FUNCTION agent_sessions.{name}()
        """)
        )
    try:
        with pytest.raises(DBAPIError, match="injected pending disposition failure"):
            _hold(lane)
        with Session(lane.engine) as session:
            assert store.get_turn(session, lane.session_id, 1) is None
            row = store.get_session(session, lane.session_id)
            assert row.status == "running"
            assert row.progress_token == lane.token
            pending = store.get_pending_message(session, lane.session_id, 1)
            assert pending.claimed_by_replica == OWNER
            assert pending.partial_text == "partial result"
            assert json.loads(pending.partial_activities) == [{"tool": "read"}]
    finally:
        with lane.engine.begin() as connection:
            connection.execute(
                text(f"DROP TRIGGER {name} ON agent_sessions.pending_messages")
            )
            connection.execute(text(f"DROP FUNCTION agent_sessions.{name}()"))
    assert _hold(lane) is True
