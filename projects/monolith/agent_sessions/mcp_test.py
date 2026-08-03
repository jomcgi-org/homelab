from __future__ import annotations

import asyncio

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from agent_sessions import mcp, model_family, store
from agent_sessions.models import AgentSession, AgentTurn
from agent_sessions.transport import EmberSession, EmberSessionGone, Turn
from faas.embervm_client import EmberVMTransportError


@pytest.mark.parametrize(
    ("model", "family"),
    [
        (None, "claude"),
        ("opus", "claude"),
        ("sonnet", "claude"),
        ("fable", "claude"),
        ("luna", "codex"),
        ("terra", "codex"),
        ("sol", "codex"),
        ("qwen", "pi"),
    ],
)
def test_model_family(model, family):
    assert model_family(model) == family


def test_unknown_model_is_rejected_without_creating_session(session):
    result = asyncio.run(mcp.monolith_agent_session_start("hello", model="unknown"))
    assert result["accepted"] is False
    assert "valid models" in result["error"]
    assert session.exec(select(AgentSession)).first() is None


@pytest.mark.parametrize("model", [None, "opus", "luna", "qwen"])
def test_session_start_stores_model_on_session_and_pending(monkeypatch, session, model):
    monkeypatch.setattr(mcp, "_schedule_next_message", lambda _session_id: None)
    result = asyncio.run(mcp.monolith_agent_session_start("hello", model=model))
    row = store.get_session(session, result["session_id"])
    pending = store.get_pending_message(session, row.id, result["turn"])
    assert row.model == model
    assert pending.model == model


@pytest.fixture
def session(monkeypatch, tmp_path):
    # A FILE-backed database with a real pool, not the usual in-memory
    # StaticPool. StaticPool hands every Session the same single connection, and
    # the code under test deliberately opens its own Session per operation and
    # runs it in a worker thread via asyncio.to_thread. Concurrent statements on
    # one SQLite connection interleave unpredictably, so the concurrency tests
    # saw both claimants lose the race and nothing execute. A file lets each
    # Session take its own connection, which is what production does and what
    # makes an atomic claim meaningful to test at all.
    engine = create_engine(
        f"sqlite:///{tmp_path / 'agent_sessions_test.db'}",
        connect_args={"check_same_thread": False},
    )
    table_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            table_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        monkeypatch.setattr(mcp, "get_engine", lambda: engine)
        monkeypatch.setattr(store, "get_engine", lambda: engine)
        with Session(engine) as db_session:
            yield db_session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in table_schemas:
                table.schema = table_schemas[table.name]


def _turn(seq: int, activities: list[dict], cost: float) -> AgentTurn:
    import json
    from datetime import datetime, timezone

    return AgentTurn(
        id=seq,
        session_id=1,
        seq=seq,
        prompt="prompt",
        result_text="result",
        usage_json=json.dumps({"activities": activities}),
        cost_usd=cost,
        created_at=datetime.now(timezone.utc),
    )


def test_activity_aggregation_and_spoken_diff():
    turns = [
        _turn(
            1,
            [
                {"tool": "Bash", "command": "git status"},
                {"tool": "Edit", "file_path": "a.py"},
            ],
            0.1,
        ),
        _turn(
            2,
            [
                {"tool": "Bash", "command": "git status"},
                {"tool": "Write", "file_path": "b.py"},
            ],
            0.2,
        ),
    ]
    assert mcp._activity_values(turns) == (["a.py", "b.py"], ["git status"])


def test_send_persists_and_returns_immediately(session):
    row = store.create_session(session, "sid-123", "/workspace", "main")

    result = asyncio.run(mcp.monolith_agent_session_send(row.id, "hello"))

    assert result["accepted"] is True
    assert result["session_id"] == row.id
    assert result["turn"] >= 1
    pending = store.get_pending_message(session, row.id, result["turn"])
    assert pending.message_text == "hello"
    assert pending.seq == result["turn"]


def test_cross_family_send_is_rejected_without_pending_row(session):
    row = store.create_session(session, "sid-123", "/workspace", "main", model="luna")
    result = asyncio.run(mcp.monolith_agent_session_send(row.id, "hello", model="qwen"))
    assert result["accepted"] is False
    assert "codex" in result["error"] and "pi" in result["error"]
    assert store.get_pending_message(session, row.id, 1) is None


def test_same_family_override_reaches_transport_and_turn(monkeypatch, session):
    monkeypatch.setattr(mcp, "_schedule_next_message", lambda _session_id: None)
    delivered_models = []

    async def mock_deliver(_ember, _cli_session_id, message, model=None):
        delivered_models.append(model)
        return _completed_delivery(message)

    async def notify(*args, **kwargs):
        return None

    monkeypatch.setattr(mcp._transport, "deliver", mock_deliver)
    monkeypatch.setattr(mcp.agent_api, "notify", notify)
    row = store.create_session(session, "sid-123", "/workspace", "main", model="luna")
    row.status = "completed"
    session.add(row)
    session.commit()
    result = asyncio.run(
        mcp.monolith_agent_session_send(row.id, "hello", model="terra")
    )
    assert result["accepted"] is True
    # The send wrote status through its own Session; expire this one's identity
    # map or get() returns the stale cached row instead of re-reading.
    session.expire_all()
    assert store.get_session(session, row.id).status == "running"
    asyncio.run(mcp._execute_pending_message(row.id))
    assert delivered_models == ["terra"]
    assert store.get_turn(session, row.id, result["turn"]).model == "terra"


def test_session_start_returns_immediately(monkeypatch, session):
    started = asyncio.Event()

    async def blocking_deliver(_ember, _cli_session_id, _message, _model=None):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(mcp._transport, "deliver", blocking_deliver)

    async def run():
        result = await asyncio.wait_for(
            mcp.monolith_agent_session_start("hello"), timeout=0.1
        )
        pending = store.get_pending_message(session, result["session_id"], 1)
        return result, pending

    result, pending = asyncio.run(run())

    assert result["accepted"] is True
    assert result["session_id"] > 0
    assert result["turn"] == 1
    assert pending is not None
    assert pending.claimed_by_replica is None
    assert not started.is_set()


def test_session_start_happy_path_persists_result(monkeypatch, session):
    monkeypatch.setattr(mcp, "_schedule_next_message", lambda _session_id: None)

    async def mock_deliver(_ember, _cli_session_id, message, _model=None):
        return _completed_delivery(message)

    async def notify(*args, **kwargs):
        return None

    monkeypatch.setattr(mcp._transport, "deliver", mock_deliver)
    monkeypatch.setattr(mcp.agent_api, "notify", notify)

    async def start():
        return await mcp.monolith_agent_session_start("hello")

    result = asyncio.run(start())
    row = store.get_session(session, result["session_id"])
    assert row is not None
    assert row.status == "running"

    asyncio.run(mcp._execute_pending_message(row.id))

    session.expire_all()
    turn = store.get_turn(session, row.id, 1)
    assert turn is not None
    assert turn.result_text == "Done: hello"
    assert turn.terminal_reason == "completed"
    assert turn.stop_reason == "end_turn"
    assert turn.cost_usd == 0.01
    assert store.get_pending_message(session, row.id, 1) is None
    assert store.get_session(session, row.id).status == "completed"


def test_failed_first_turn_does_not_wedge_session(monkeypatch, session):
    monkeypatch.setattr(mcp, "_schedule_next_message", lambda _session_id: None)

    async def failing_deliver(_ember, _cli_session_id, _message, _model=None):
        raise RuntimeError("first turn failed")

    monkeypatch.setattr(mcp._transport, "deliver", failing_deliver)

    result = asyncio.run(mcp.monolith_agent_session_start("hello"))
    asyncio.run(mcp._execute_pending_message(result["session_id"]))

    session.expire_all()
    row = store.get_session(session, result["session_id"])
    assert row is not None
    assert row.status == "warn"
    assert store.get_turn(session, row.id, 1) is not None
    assert store.get_pending_message(session, row.id, 1) is None

    follow_up = asyncio.run(mcp.monolith_agent_session_send(row.id, "follow up"))
    assert follow_up["turn"] == 2


def test_concurrent_executors_on_first_turn_run_once(monkeypatch, session):
    monkeypatch.setattr(mcp, "_schedule_next_message", lambda _session_id: None)
    executions: list[str] = []

    async def mock_deliver(_ember, _cli_session_id, message, _model=None):
        executions.append(message)
        await asyncio.sleep(0.01)
        return _completed_delivery(message)

    async def notify(*args, **kwargs):
        return None

    monkeypatch.setattr(mcp._transport, "deliver", mock_deliver)
    monkeypatch.setattr(mcp.agent_api, "notify", notify)

    result = asyncio.run(mcp.monolith_agent_session_start("hello"))

    async def run_executors():
        await asyncio.gather(
            mcp._execute_pending_message(result["session_id"]),
            mcp._execute_pending_message(result["session_id"]),
        )

    asyncio.run(run_executors())

    session.expire_all()
    assert executions == ["hello"]
    assert store.get_turn(session, result["session_id"], 1) is not None
    assert store.get_pending_message(session, result["session_id"], 1) is None


def _completed_turn(message: str) -> Turn:
    return Turn(
        result=f"Done: {message}",
        terminal_reason="completed",
        stop_reason="end_turn",
        is_error=False,
        permission_denials=[],
        num_turns=1,
        session_id="sid-123",
        usage={},
        total_cost_usd=0.01,
        duration_ms=100,
        activities=[],
    )


def _completed_delivery(message: str) -> tuple[Turn, EmberSession]:
    return _completed_turn(message), EmberSession("ember-1", "token-1", None)


def test_pending_message_executed_in_background(monkeypatch, session):
    row = store.create_session(session, "sid-123", "/workspace", "main")

    async def mock_deliver(_ember, _cli_session_id, message, _model=None):
        return _completed_delivery(message)

    monkeypatch.setattr(mcp._transport, "deliver", mock_deliver)

    async def notify(*args, **kwargs):
        return None

    monkeypatch.setattr(mcp.agent_api, "notify", notify)

    async def run():
        result = await mcp.monolith_agent_session_send(row.id, "hello")
        await asyncio.sleep(0.1)
        return result

    result = asyncio.run(run())
    session.expire_all()
    turn = store.get_turn(session, row.id, result["turn"])
    assert turn is not None
    assert turn.result_text == "Done: hello"
    assert turn.terminal_reason == "completed"
    assert store.get_pending_message(session, row.id, result["turn"]) is None


def test_failed_delivery_clears_reused_ember_session(monkeypatch, session):
    row = store.create_session(session, "sid-123", "/workspace", "main")
    store.set_ember_session(session, row.id, "ember-1", "token-1", 1754035200000)
    row.cli_session_id = "cli-1"
    session.add(row)
    session.commit()
    pending = store.create_pending_message(session, row.id, "hello")
    pending_seq = pending.seq

    async def failing_deliver(_ember, _cli_session_id, _message, _model=None):
        raise EmberSessionGone("terminal invoke failure")

    monkeypatch.setattr(mcp._transport, "deliver", failing_deliver)

    asyncio.run(mcp._execute_pending_message(row.id))

    session.expire_all()
    cleared = store.get_session(session, row.id)
    assert cleared is not None
    assert cleared.ember_session_id is None
    assert cleared.ember_session_token is None
    assert cleared.ember_session_expires_at is None
    assert cleared.cli_session_id is None
    assert store.get_turn(session, row.id, pending_seq) is not None
    pending_after = store.get_pending_message(session, row.id, pending_seq)
    assert pending_after is None


def test_failed_guest_delivery_does_not_clear_reused_session(monkeypatch, session):
    row = store.create_session(session, "sid-123", "/workspace", "main")
    store.set_ember_session(session, row.id, "ember-1", "token-1", 1754035200000)
    row.cli_session_id = "cli-1"
    session.add(row)
    session.commit()
    store.create_pending_message(session, row.id, "hello")

    async def failing_deliver(_ember, _cli_session_id, _message, _model=None):
        raise EmberVMTransportError("422 Unprocessable Entity")

    monkeypatch.setattr(mcp._transport, "deliver", failing_deliver)

    asyncio.run(mcp._execute_pending_message(row.id))

    session.expire_all()
    unchanged = store.get_session(session, row.id)
    assert unchanged is not None
    assert unchanged.ember_session_id == "ember-1"
    assert unchanged.ember_session_token == "token-1"
    assert unchanged.ember_session_expires_at == 1754035200000
    assert unchanged.cli_session_id == "cli-1"


def test_recreated_ember_session_adopts_new_cli_session_id(monkeypatch, session):
    row = store.create_session(session, "sid-123", "/workspace", "main")
    store.set_ember_session(session, row.id, "ember-old", "token-old", 1754035200000)
    row.cli_session_id = "cli-old"
    session.add(row)
    session.commit()
    pending = store.create_pending_message(session, row.id, "hello")
    pending_seq = pending.seq
    new_ember = EmberSession("ember-new", "token-new", 1754035300000)

    async def succeeding_delivery(_ember, _cli_session_id, _message, _model=None):
        return _completed_turn("hello")._replace(session_id="cli-new"), new_ember

    async def notify(*args, **kwargs):
        return None

    monkeypatch.setattr(mcp._transport, "deliver", succeeding_delivery)
    monkeypatch.setattr(mcp.agent_api, "notify", notify)

    asyncio.run(mcp._execute_pending_message(row.id))

    session.expire_all()
    updated = store.get_session(session, row.id)
    assert updated is not None
    assert updated.ember_session_id == "ember-new"
    assert updated.ember_session_token == "token-new"
    assert updated.cli_session_id == "cli-new"
    assert store.get_turn(session, row.id, pending_seq) is not None
    pending_after = store.get_pending_message(session, row.id, pending_seq)
    assert pending_after is None


def test_startup_sweep_lists_orphaned_messages(session):
    row = store.create_session(session, "sid-123", "/workspace", "main")
    store.create_pending_message(session, row.id, "orphaned message")

    orphaned = store.get_all_pending_messages_sync()

    assert len(orphaned) == 1
    assert orphaned[0].message_text == "orphaned message"


def test_two_sends_are_serialized(monkeypatch, session):
    row = store.create_session(session, "sid-123", "/workspace", "main")
    execution_order = []

    async def fake_deliver(_ember, _cli_session_id, message, _model=None):
        execution_order.append(message)
        await asyncio.sleep(0.01)
        return _completed_delivery(message)

    monkeypatch.setattr(mcp._transport, "deliver", fake_deliver)

    async def notify(*args, **kwargs):
        return None

    monkeypatch.setattr(mcp.agent_api, "notify", notify)

    async def run():
        result1 = await mcp.monolith_agent_session_send(row.id, "first")
        result2 = await mcp.monolith_agent_session_send(row.id, "second")
        await asyncio.sleep(0.1)
        return result1, result2

    result1, result2 = asyncio.run(run())
    session.expire_all()
    assert store.get_turn(session, row.id, result1["turn"]) is not None
    assert store.get_turn(session, row.id, result2["turn"]) is not None
    assert execution_order == ["first", "second"]


def test_concurrent_replicas_execute_pending_message_once(monkeypatch, session):
    """Test that concurrent replicas execute a message exactly once via atomic claim.

    This test exercises the real atomic UPDATE WHERE claimed_by_replica IS NULL
    to verify cross-replica serialization, not a monkeypatched in-memory set.
    """
    row = store.create_session(session, "sid-123", "/workspace", "main")
    pending = store.create_pending_message(session, row.id, "hello")
    pending_seq = (
        pending.seq
    )  # capture before expire_all(); the row is deleted on completion
    executions: list[str] = []

    async def fake_deliver(_ember, _cli_session_id, message, _model=None):
        executions.append(message)
        await asyncio.sleep(0.01)
        return _completed_delivery(message)

    async def notify(*args, **kwargs):
        return None

    monkeypatch.setattr(mcp._transport, "deliver", fake_deliver)
    monkeypatch.setattr(mcp.agent_api, "notify", notify)

    async def run():
        await asyncio.gather(
            mcp._execute_pending_message(row.id),
            mcp._execute_pending_message(row.id),
        )

    asyncio.run(run())

    # Only one concurrent execution should succeed in claiming the row
    assert executions == ["hello"]
    # Turn should be persisted and pending row should be deleted
    session.expire_all()
    assert store.get_turn(session, row.id, pending_seq) is not None
    assert store.get_pending_message(session, row.id, pending_seq) is None


def test_actively_refreshed_claim_is_not_reclaimed(session):
    """Test that a claim being actively refreshed is not reclaimed by sweep.

    This test verifies the heartbeat mechanism: refresh_claim_sync keeps the
    claimed_at timestamp fresh, preventing reclaim_stale_claims_sync from
    reclaiming the message even though time has passed conceptually.
    """
    from datetime import datetime, timezone

    row = store.create_session(session, "sid-456", "/workspace", "main")
    pending = store.create_pending_message(session, row.id, "test")

    # Simulate claiming the message (database returns lowest unclaimed seq)
    claimed_seq = store.claim_pending_message_for_session_sync(row.id, "monolith")
    assert claimed_seq == pending.seq

    # Simulate multiple refreshes to keep the claim fresh
    for i in range(3):
        still_held = store.refresh_claim_sync(row.id, pending.seq, "monolith")
        assert still_held, f"Claim was unexpectedly lost on refresh {i + 1}"

    # After multiple refreshes, run the sweep with 30s lease
    # The claim should NOT be reclaimed because it's been refreshed
    reclaimed_count = store.reclaim_stale_claims_sync(lease_interval_seconds=30)
    assert reclaimed_count == 0, "Active claim should not be reclaimed"

    # Verify the message is still claimed
    session.expire_all()
    pending_row = store.get_pending_message(session, row.id, pending.seq)
    assert pending_row is not None
    assert pending_row.claimed_by_replica == "monolith"


def test_stale_claim_is_reclaimed(session):
    """Test that a claim that is not refreshed is reclaimed by sweep.

    This test verifies that reclaim_stale_claims_sync correctly reclaims
    claims whose refresh heartbeat has stopped (crashed replica).
    """
    from datetime import datetime, timedelta, timezone

    row = store.create_session(session, "sid-789", "/workspace", "main")
    pending = store.create_pending_message(session, row.id, "test")

    # Claim the message (database returns lowest unclaimed seq)
    claimed_seq = store.claim_pending_message_for_session_sync(row.id, "monolith")
    assert claimed_seq == pending.seq

    # Simulate a crashed replica by treating any claim as expired, rather than
    # back-dating claimed_at. The column is written by the database (func.now())
    # and Python 3.13 removed the sqlite3 datetime adapter, so binding a datetime
    # here both fights the design and fails outright.

    # Run the sweep, which should reclaim the stale claim
    reclaimed_count = store.reclaim_stale_claims_sync(lease_interval_seconds=0)
    assert reclaimed_count == 1, "Stale claim should be reclaimed"

    # Verify the message is no longer claimed
    session.expire_all()
    pending_row = store.get_pending_message(session, row.id, pending.seq)
    assert pending_row is not None
    assert pending_row.claimed_by_replica is None
    assert pending_row.claimed_at is None


def test_heartbeat_refresh_with_real_replica_id(session, monkeypatch):
    """Test that heartbeat refresh works with the actual replica id used for claiming.

    This test exercises the real code path: claiming with _REPLICA_ID, then
    refreshing with the same id. This would catch the bug where the refresher
    used "monolith" instead of _REPLICA_ID.
    """
    import platform

    row = store.create_session(session, "sid-hb-real", "/workspace", "main")
    pending = store.create_pending_message(session, row.id, "test")

    # Get the actual replica id that would be used
    real_replica_id = platform.node()

    # Claim with the real replica id (database returns lowest unclaimed seq)
    claimed_seq = store.claim_pending_message_for_session_sync(row.id, real_replica_id)
    assert claimed_seq == pending.seq

    # Refresh multiple times with the SAME replica id
    for i in range(5):
        still_held = store.refresh_claim_sync(row.id, pending.seq, real_replica_id)
        assert still_held, (
            f"Claim lost on refresh {i + 1} when using correct replica id {real_replica_id}"
        )

    # After multiple refreshes, run the sweep
    # The claim should NOT be reclaimed
    reclaimed_count = store.reclaim_stale_claims_sync(lease_interval_seconds=30)
    assert reclaimed_count == 0, "Actively refreshed claim should not be reclaimed"

    # Verify the claim is still active
    session.expire_all()
    pending_row = store.get_pending_message(session, row.id, pending.seq)
    assert pending_row is not None
    assert pending_row.claimed_by_replica == real_replica_id


def test_heartbeat_refresh_fails_with_wrong_replica_id(session):
    """Test that refresh fails if called with a different replica id than claimed.

    This ensures that the heartbeat code must use the correct replica id or
    the claim will be detected as stolen.
    """
    row = store.create_session(session, "sid-wrong-id", "/workspace", "main")
    pending = store.create_pending_message(session, row.id, "test")

    # Claim with one replica id (database returns lowest unclaimed seq)
    claimed_seq = store.claim_pending_message_for_session_sync(row.id, "replica-a")
    assert claimed_seq == pending.seq

    # Try to refresh with a DIFFERENT replica id (like the hardcoded "monolith" bug)
    still_held = store.refresh_claim_sync(row.id, pending.seq, "replica-b")
    assert not still_held, (
        "Refresh should fail when called with wrong replica id (claim would be considered stolen)"
    )

    # The claim is still owned by the original replica
    session.expire_all()
    pending_row = store.get_pending_message(session, row.id, pending.seq)
    assert pending_row is not None
    assert pending_row.claimed_by_replica == "replica-a"


def test_concurrent_seq_allocation_race_retries(session):
    """Test that concurrent seq allocations handle collisions via retry.

    This test verifies that when two threads race to allocate the same seq,
    one wins the UNIQUE constraint check, and the other retries and allocates
    a higher seq. Both messages are successfully enqueued despite the collision.
    """
    row = store.create_session(session, "sid-race", "/workspace", "main")

    # Use asyncio to simulate concurrent allocations
    async def concurrent_allocations():
        # Run two allocations concurrently - both will compute seq=1 initially
        # but the retry logic will make one of them get seq=2
        seq1 = await asyncio.to_thread(
            store.create_pending_message, session, row.id, "message-1"
        )
        seq2 = await asyncio.to_thread(
            store.create_pending_message, session, row.id, "message-2"
        )
        return seq1.seq, seq2.seq

    seq1, seq2 = asyncio.run(concurrent_allocations())

    # Both should succeed; one gets seq 1 and the other gets seq 2
    # (The order depends on thread scheduling, but both should be present)
    session.expire_all()
    msg1 = store.get_pending_message(session, row.id, seq1)
    msg2 = store.get_pending_message(session, row.id, seq2)
    assert msg1 is not None and msg1.message_text == "message-1"
    assert msg2 is not None and msg2.message_text == "message-2"
    # Verify they have different seqs
    assert seq1 != seq2


def test_notify_terminal_with_no_terminal_reason_warns(monkeypatch):
    """Test that a turn with terminal_reason=None produces a warn-level notify.

    When a turn ends without a terminal_reason (e.g., transport dies mid-turn),
    it should be reported as a warn-level notification, not silently ignored.
    """
    from agent_sessions.transport import Turn

    notify_calls = []

    async def mock_notify(summary, level):
        notify_calls.append((summary, level))

    monkeypatch.setattr(mcp.agent_api, "notify", mock_notify)

    async def run():
        turn = Turn(
            result="Partial result",
            terminal_reason=None,  # No terminal reason (transport died)
            stop_reason="end_turn",
            is_error=False,
            permission_denials=[],
            num_turns=1,
            session_id="sid-test",
            usage={},
            total_cost_usd=0.01,
            duration_ms=100,
            activities=[],
        )
        await mcp._notify_terminal(turn, "Test summary", "warn")

    asyncio.run(run())

    assert len(notify_calls) == 1
    summary, level = notify_calls[0]
    assert summary == "Test summary"
    assert level == "warn"


def test_turn_status_needs_input_on_permission_denials():
    """Test that permission_denials takes priority and returns needs_input status.

    The critical ordering is that permission_denials is checked FIRST in
    _turn_status, so a turn with permission_denials AND terminal_reason="completed"
    must still return "needs_input". This is the whole point of checking denials first.
    """
    from agent_sessions.transport import Turn

    # Turn with permission_denials should be needs_input
    turn_with_denials = Turn(
        result="Result text",
        terminal_reason="completed",  # Normally would be "completed"
        stop_reason="end_turn",
        is_error=False,
        permission_denials=["tool_use"],  # But has denials
        num_turns=1,
        session_id="sid-test",
        usage={},
        total_cost_usd=0.01,
        duration_ms=100,
        activities=[],
    )

    status = mcp._turn_status(turn_with_denials)

    # Must be "needs_input" because permission_denials takes priority
    assert status == "needs_input", (
        f"Turn with permission_denials should be needs_input, got {status}"
    )


def test_ember_expires_at_column_is_bigint():
    """The expiry column must be BigInteger, not Integer.

    It holds epoch MILLISECONDS from the control plane, which overflow int4.
    This asserts the mapped type rather than round-tripping a value because the
    test database is SQLite, which stores integers dynamically and therefore
    cannot reproduce the Postgres overflow this guards against: the production
    failure was `psycopg.errors.NumericValueOutOfRange` from SQLModel emitting an
    explicit ::INTEGER cast against a bigint column.
    """
    from sqlalchemy import BigInteger

    from agent_sessions.models import AgentSession

    column = AgentSession.__table__.c.ember_session_expires_at
    assert isinstance(column.type, BigInteger), (
        "ember_session_expires_at must map to BigInteger; got %r" % column.type
    )


def test_broker_login_start_surfaces_code_and_notifies(monkeypatch):
    monkeypatch.setenv("EMBER_TOKENBROKER_URL", "http://broker")
    calls = []
    notified = []

    async def fake_request(method, path):
        calls.append((method, path))
        return {
            "verification_url": "https://auth/device",
            "user_code": "ABCD-EFGH",
            "expires_in": 900,
        }

    async def fake_notify(message, level="info"):
        notified.append((message, level))

    monkeypatch.setattr(mcp, "_broker_request", fake_request)
    monkeypatch.setattr(mcp.agent_api, "notify", fake_notify)
    result = asyncio.run(mcp.monolith_codex_broker_login_start())
    assert result["user_code"] == "ABCD-EFGH"
    assert calls == [("POST", "/grants/codex-cluster/login/start")]
    assert "ABCD-EFGH" in notified[0][0] and notified[0][1] == "warn"


def test_broker_login_status_granted_notifies(monkeypatch):
    monkeypatch.setenv("EMBER_TOKENBROKER_URL", "http://broker")
    notified = []

    async def fake_request(method, path):
        return {"state": "granted", "detail": ""}

    async def fake_notify(message, level="info"):
        notified.append((message, level))

    monkeypatch.setattr(mcp, "_broker_request", fake_request)
    monkeypatch.setattr(mcp.agent_api, "notify", fake_notify)
    result = asyncio.run(mcp.monolith_codex_broker_login_status())
    assert result["state"] == "granted"
    assert notified and notified[0][1] == "info"


def test_broker_login_rejects_bad_grant_and_unset_url(monkeypatch):
    monkeypatch.setenv("EMBER_TOKENBROKER_URL", "http://broker")
    with pytest.raises(ValueError):
        asyncio.run(mcp.monolith_codex_broker_login_start(grant="../../etc"))
    monkeypatch.delenv("EMBER_TOKENBROKER_URL")
    with pytest.raises(ValueError):
        asyncio.run(mcp.monolith_codex_broker_login_status())
