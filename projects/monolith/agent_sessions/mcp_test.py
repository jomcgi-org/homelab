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
    assert mcp._spoken_diff("3 files changed, 42 insertions(+), 8 deletions(-)") == (
        "3 files changed, 42 insertions(+), 8 deletions(-)"
    )


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
    monkeypatch.setattr(
        mcp._transport, "deliver", lambda _session_id, message: _completed_turn(message)
    )

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
            mcp._execute_pending_message(row.id, pending.seq),
            mcp._execute_pending_message(row.id, pending.seq),
        )

    asyncio.run(run())

    # Only one concurrent execution should succeed in claiming the row
    assert executions == ["hello"]
    # Turn should be persisted and pending row should be deleted
    assert store.get_turn(session, row.id, pending.seq) is not None
    assert store.get_pending_message(session, row.id, pending.seq) is None
