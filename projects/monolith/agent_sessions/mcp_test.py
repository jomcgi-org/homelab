from __future__ import annotations

import asyncio

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from agent_sessions import mcp
from agent_sessions import store
from agent_sessions.models import AgentTurn


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
