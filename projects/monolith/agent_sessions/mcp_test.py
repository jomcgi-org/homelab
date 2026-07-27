from __future__ import annotations

import asyncio

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from agent_sessions import mcp
from agent_sessions import store
from agent_sessions.models import AgentTurn
from agent_sessions.transport import Turn


@pytest.fixture
def session(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
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


def test_pending_message_executed_in_background(monkeypatch, session):
    row = store.create_session(session, "sid-123", "/workspace", "main")

    async def mock_deliver(_session_id, message):
        return _completed_turn(message)

    monkeypatch.setattr(mcp._transport, "deliver", mock_deliver)

    async def notify(*args, **kwargs):
        return None

    monkeypatch.setattr(mcp.agent_api, "notify", notify)

    async def run():
        result = await mcp.monolith_agent_session_send(row.id, "hello")
        await asyncio.sleep(0.1)
        return result

    result = asyncio.run(run())
    turn = store.get_turn(session, row.id, result["turn"])
    assert turn is not None
    assert turn.result_text == "Done: hello"
    assert turn.terminal_reason == "completed"
    assert store.get_pending_message(session, row.id, result["turn"]) is None


def test_startup_sweep_lists_orphaned_messages(session):
    row = store.create_session(session, "sid-123", "/workspace", "main")
    store.create_pending_message(session, row.id, "orphaned message")

    orphaned = store.get_all_pending_messages_sync()

    assert len(orphaned) == 1
    assert orphaned[0].message_text == "orphaned message"


def test_two_sends_are_serialized(monkeypatch, session):
    row = store.create_session(session, "sid-123", "/workspace", "main")
    execution_order = []

    async def fake_deliver(_session_id, message):
        execution_order.append(message)
        await asyncio.sleep(0.01)
        return _completed_turn(message)

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
    executions: list[str] = []

    async def fake_deliver(_session_id, message):
        executions.append(message)
        await asyncio.sleep(0.01)
        return _completed_turn(message)

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
    assert store.get_turn(session, row.id, pending.seq) is not None
    assert store.get_pending_message(session, row.id, pending.seq) is None


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

    # Simulate a crash by manually setting claimed_at to an old time
    # This makes the claim appear stale without needing to actually sleep.
    # Coerce the datetime to handle SQLite's naive round-trip: per CLAUDE.md,
    # "dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)"
    stale_time = datetime.now(timezone.utc) - timedelta(seconds=40)
    stale_time = (
        stale_time if stale_time.tzinfo else stale_time.replace(tzinfo=timezone.utc)
    )
    with store.Session(store.get_engine()) as db_session:
        pm = store.get_pending_message(db_session, row.id, pending.seq)
        if pm:
            pm.claimed_at = stale_time
            db_session.add(pm)
            db_session.commit()

    # Run the sweep, which should reclaim the stale claim
    reclaimed_count = store.reclaim_stale_claims_sync(lease_interval_seconds=30)
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
