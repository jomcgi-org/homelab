"""Real process loss with PostgreSQL and an independently surviving remote.

This composes executor, HTTP transport, progress ingestion and recovery sweep.
It does not run EmberVM, leader election, DBOS, or a deployed application image.
Run only through the registered Linux BDD target.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from threading import Event
from time import monotonic
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete
from sqlalchemy.pool import NullPool
from sqlmodel import Session, create_engine, select

from agent_sessions import store
from agent_sessions.models import AgentSession, AgentTurn, PendingMessage
from agent_sessions.process_crash_driver_test import (
    ACTIVITIES,
    CHILD_ENV_KEYS,
    PARTIAL,
    PROMPT,
)
from agent_sessions.transport import Turn


def _wait(predicate, description, timeout=15):
    deadline = monotonic() + timeout
    poll = Event()
    while monotonic() < deadline:
        result = predicate()
        if result:
            return result
        poll.wait(0.05)
    pytest.fail(f"timed out observing {description}")


@contextmanager
def _child(tmp_path, environment, role, argument, pass_fds=()):
    log_path = tmp_path / f"{role}-{uuid4().hex[:8]}.log"
    with log_path.open("w+") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-P",
                "-m",
                "agent_sessions.process_crash_driver_test",
                role,
                str(argument),
            ],
            cwd=tmp_path,
            env=environment,
            pass_fds=pass_fds,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            log.seek(0)
            print(f"{role} child {process.pid}:\n{log.read()[-12000:]}")


@pytest.fixture
def crash_lane(pg, tmp_path, monkeypatch):
    engine = create_engine(
        pg.url,
        poolclass=NullPool,
        connect_args={
            "connect_timeout": 5,
            "options": "-c statement_timeout=10000 -c lock_timeout=5000",
        },
    )
    monkeypatch.setattr(store, "get_engine", lambda: engine)
    ids = []

    def create(prompt):
        with Session(engine) as session:
            row = store.create_session(session, f"crash-{uuid4()}", "<guest>", "main")
            identity = row.id
            ids.append(identity)
            store.create_pending_message(session, identity, prompt, "luna")
            return identity

    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            url = f"http://127.0.0.1:{listener.getsockname()[1]}"
            # An explicit allowlist, never os.environ.copy(). All URLs and
            # credentials below belong to this fresh test's loopback resources.
            # Pytest can prepend the test directory. Keep its package root,
            # so agent_sessions/mcp.py cannot shadow the installed mcp package.
            test_directory = Path(__file__).parent.resolve()
            test_directories = {test_directory, Path(__file__).resolve().parent}
            import_paths = [str(test_directory.parent)] + [
                str(Path(p).resolve())
                for p in sys.path
                if p and Path(p).resolve() not in test_directories
            ]
            environment = {
                "PATH": "/usr/bin:/bin",
                "LC_ALL": "C.UTF-8",
                "PYTHONPATH": os.pathsep.join(import_paths),
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUTF8": "1",
                "DATABASE_URL": pg.url,
                "EMBERVM_URL": url,
                "OTEL_SDK_DISABLED": "true",
                "AGENT_SESSIONS_CHANNEL_NOTIFY": "none",
                "PGCONNECT_TIMEOUT": "5",
                "PGCLIENTENCODING": "UTF8",
                "PGOPTIONS": "-c statement_timeout=10000 -c lock_timeout=5000",
            }
            assert set(environment) == CHILD_ENV_KEYS
            with _child(
                tmp_path,
                environment,
                "control",
                listener.fileno(),
                (listener.fileno(),),
            ) as remote:
                with httpx.Client(base_url=url, timeout=2, trust_env=False) as client:

                    def state():
                        assert remote.poll() is None, "remote stand-in exited"
                        try:
                            response = client.get("/test/state")
                        except httpx.TransportError:
                            return None
                        response.raise_for_status()
                        return response.json()

                    _wait(state, "control-plane startup")
                    yield engine, create, environment, client, state
    finally:
        with engine.begin() as connection:
            for model in (PendingMessage, AgentTurn, AgentSession):
                column = model.id if model is AgentSession else model.session_id
                connection.execute(delete(model).where(column.in_(ids)))
        engine.dispose()


def test_observer_kill_preserves_unknown_outcome_without_remote_replay(
    crash_lane, tmp_path
):
    engine, create, environment, client, remote_state = crash_lane
    identity = create(PROMPT)
    with Session(engine) as session:
        store.create_pending_message(session, identity, "held successor", "luna")

    with _child(tmp_path, environment, "observer", identity) as observer:

        def admitted():
            assert observer.poll() is None, "observer exited before remote admission"
            invocations = remote_state()["invocations"]
            return invocations and invocations[0].get("progress_status") == 204

        _wait(admitted, "real invoke admission and durable progress")
        with Session(engine) as session:
            row = store.get_session(session, identity)
            pending = store.get_pending_message(session, identity, 1)
            assert pending.dispatch_count == 1
            assert pending.partial_text == PARTIAL
            assert json.loads(pending.partial_activities) == ACTIVITIES
            owner = pending.claimed_by_replica
            dispatched = pending.last_dispatch_at
            heartbeat = pending.claimed_at
            binding = (
                row.ember_session_id,
                row.ember_lineage_id,
                row.ember_session_token,
            )
            assert all(binding) and owner and dispatched and heartbeat
            assert store.get_turns(session, identity) == []
        observer.kill()
        assert observer.wait(timeout=5) == -signal.SIGKILL

    def held():
        with Session(engine) as session:
            turn = store.get_turn(session, identity, 1)
            if turn is None:
                return False
            assert turn.stop_reason == store.UNKNOWN_INVOCATION
            # Read the atomic disposition in the same database transaction.
            assert store.get_pending_message(session, identity, 1) is None
            assert store.get_session(session, identity).status == "failed"
            return True

    def snapshot():
        with Session(engine) as session:
            row = store.get_session(session, identity)
            turns = store.get_turns(session, identity)
            successor = store.get_pending_message(session, identity, 2)
            assert (
                len(
                    session.exec(
                        select(PendingMessage).where(
                            PendingMessage.session_id == identity
                        )
                    ).all()
                )
                == 1
            )
            assert len(turns) == 1
            turn = turns[0]
            assert turn.prompt == PROMPT and turn.result_text == PARTIAL
            assert (
                turn.terminal_reason == "error"
                and turn.stop_reason == store.UNKNOWN_INVOCATION
            )
            assert turn.cost_usd is None
            usage = json.loads(turn.usage_json)
            assert usage["activities"] == ACTIVITIES
            recovery = usage["recovery"]
            assert recovery["cause"] == "lease_expired"
            assert recovery["dispatch_count"] == 1
            assert recovery["claim_owner"] == owner
            assert recovery["last_dispatch_at"] == dispatched.isoformat()
            assert recovery["partial_text"] == PARTIAL
            assert json.loads(recovery["partial_activities"]) == ACTIVITIES
            assert row.status == "failed" and row.progress_token is None
            assert (
                row.ember_session_id,
                row.ember_lineage_id,
                row.ember_session_token,
            ) == binding
            assert successor.message_text == "held successor"
            assert (
                successor.dispatch_count == 0 and successor.claimed_by_replica is None
            )
            assert successor.last_dispatch_at is None and successor.partial_text is None
            return turn.model_dump(), row.model_dump(), successor.model_dump()

    first_marker = tmp_path / "first-sweep.json"
    with _child(tmp_path, environment, "recovery", first_marker):
        _wait(held, "lease-expiry reconciliation", timeout=60)
        assert (
            datetime.now(timezone.utc) - heartbeat.replace(tzinfo=timezone.utc)
            >= store.RECLAIM_LEASE
        )
        original = snapshot()
        assert len(remote_state()["invocations"]) == 1
        client.post("/test/release").raise_for_status()
        completed = _wait(
            lambda: remote_state()["invocations"][0].get("result"),
            "remote completion after observer death",
        )
        assert remote_state()["invocations"][0]["late_progress_status"] == 401
        # Deliver the late result to the real persistence boundary with the
        # dead attempt's original owner/count. This does not issue an invoke.
        with pytest.raises(store.SessionOutcomeUnknown):
            store.persist_turn_from_pending_sync(
                identity,
                1,
                PROMPT,
                Turn(**completed),
                "late completion",
                "completed",
                completed["session_id"],
                "luna",
                owner,
                1,
            )
        with Session(engine) as session:
            with pytest.raises(store.SessionOutcomeUnknown, match="new session"):
                store.create_pending_message(session, identity, "blocked send", "luna")
        assert snapshot() == original

    second_marker = tmp_path / "second-sweep.json"
    untouched = create("unrelated untouched admission")
    with Session(engine) as session:
        pending = store.get_pending_message(session, untouched, 1)
        assert pending.dispatch_count == 0 and pending.claimed_by_replica is None
        assert pending.last_dispatch_at is None and pending.partial_text is None
    with _child(tmp_path, environment, "recovery", second_marker) as restarted:

        def two_polls():
            assert restarted.poll() is None, "restarted recovery process exited"
            if not second_marker.exists():
                return False
            return json.loads(second_marker.read_text())["polls"] >= 2

        _wait(two_polls, "two real sweeps after recovery restart", timeout=25)

        def unrelated_completed():
            with Session(engine) as session:
                turn = store.get_turn(session, untouched, 1)
                if turn is None:
                    return False
                assert turn.terminal_reason == "completed"
                assert store.get_pending_message(session, untouched, 1) is None
                return True

        _wait(unrelated_completed, "untouched admission through restarted sweep")
        assert snapshot() == original
        state = remote_state()
        assert len(state["creates"]) == 2
        assert [item["message"] for item in state["invocations"]] == [
            PROMPT,
            "unrelated untouched admission",
        ]
        assert state["deletes"] == [] and state["unexpected"] == []
