from __future__ import annotations

import base64
import json
import zlib
from datetime import datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine

from agent_sessions import store
from agent_sessions.transport import Turn
from agent_sessions.voice import extract_voice_summary


@pytest.fixture
def session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'store_voice_test.db'}",
        connect_args={"check_same_thread": False},
    )
    table_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            table_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as db_session:
            yield db_session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in table_schemas:
                table.schema = table_schemas[table.name]


def test_session_and_turn_round_trip(session):
    row = store.create_session(session, "local-1", "/workspace", "main")
    assert store.get_session(session, row.id).local_session_id == "local-1"
    assert store.get_session_by_local_id(session, "local-1").id == row.id
    turn = store.create_turn(
        session,
        row.id,
        1,
        "fix it",
        "Done",
        "<voice>Done</voice>",
        "completed",
        "end_turn",
        [{"tool": "Bash"}],
        "abc123",
        {"input_tokens": 3, "activities": [{"tool": "Bash", "command": "git status"}]},
        0.25,
        diff_blob=b"compressed",
        diff_truncated=False,
        diff_base_sha="def456",
    )
    assert json.loads(turn.permission_denials) == [{"tool": "Bash"}]
    assert json.loads(turn.usage_json)["input_tokens"] == 3
    assert isinstance(turn.created_at, datetime)
    assert store.get_turn(session, row.id, 1).commit_sha == "abc123"
    assert turn.diff_blob == b"compressed"
    assert turn.diff_truncated is False
    assert turn.diff_base_sha == "def456"
    assert len(store.get_turns(session, row.id)) == 1

    truncated = store.create_turn(
        session,
        row.id,
        2,
        "large change",
        "Done",
        "Done",
        "completed",
        "end_turn",
        [],
        None,
        {},
        0.0,
        diff_blob=None,
        diff_truncated=True,
        diff_base_sha="fedcba",
    )
    assert truncated.diff_blob is None
    assert truncated.diff_truncated is True
    assert truncated.diff_base_sha == "fedcba"


def test_persist_turn_keeps_blob_when_diff_is_truncated(session, monkeypatch):
    agent = store.create_session(session, "local-2", "/workspace", "main")
    agent_id = agent.id
    session.commit()
    compressed = zlib.compress(b"diff --git a/plan.json b/plan.json\n")
    monkeypatch.setattr(store, "get_engine", session.get_bind)
    turn = Turn(
        result="Done",
        terminal_reason="completed",
        stop_reason="end_turn",
        is_error=False,
        permission_denials=[],
        num_turns=1,
        session_id="cli-1",
        usage={},
        total_cost_usd=0.0,
        duration_ms=1,
        activities=[],
        diff={
            "base_sha": "a" * 40,
            "zlib_b64": base64.b64encode(compressed).decode("ascii"),
            "truncated": True,
        },
    )

    store.persist_turn_from_pending_sync(
        agent_id, 1, "save the plan", turn, "Done", "completed"
    )

    session.expire_all()
    persisted = store.get_turn(session, agent_id, 1)
    assert persisted is not None
    assert persisted.diff_blob == compressed
    assert persisted.diff_truncated is True
    assert persisted.diff_base_sha == "a" * 40


def test_persist_turn_keeps_declared_artifact_bytes(session, monkeypatch):
    agent = store.create_session(session, "local-artifact", "/workspace", "main")
    agent_id = agent.id
    session.commit()
    raw = b'{"nodes": []}'
    monkeypatch.setattr(store, "get_engine", session.get_bind)
    turn = Turn(
        result="Done",
        terminal_reason="completed",
        stop_reason="end_turn",
        is_error=False,
        permission_denials=[],
        num_turns=1,
        session_id="cli-1",
        usage={},
        total_cost_usd=0.0,
        duration_ms=1,
        activities=[],
        artifact={
            "path": "plan.json",
            "content_b64": base64.b64encode(raw).decode("ascii"),
            "outcome": "ok",
        },
    )

    store.persist_turn_from_pending_sync(
        agent_id, 1, "save the plan", turn, "Done", "completed"
    )

    session.expire_all()
    persisted = store.get_turn(session, agent_id, 1)
    assert persisted.artifact_path == "plan.json"
    assert persisted.artifact_blob == raw
    assert persisted.artifact_outcome == "ok"


def test_persist_turn_discards_malformed_artifact(session, monkeypatch):
    agent = store.create_session(session, "local-bad-artifact", "/workspace", "main")
    agent_id = agent.id
    session.commit()
    monkeypatch.setattr(store, "get_engine", session.get_bind)
    turn = Turn(
        result="Done",
        terminal_reason="completed",
        stop_reason="end_turn",
        is_error=False,
        permission_denials=[],
        num_turns=1,
        session_id="cli-1",
        usage={},
        total_cost_usd=0.0,
        duration_ms=1,
        activities=[],
        artifact={"path": "plan.json"},
    )

    store.persist_turn_from_pending_sync(
        agent_id, 1, "save the plan", turn, "Done", "completed"
    )

    session.expire_all()
    persisted = store.get_turn(session, agent_id, 1)
    assert persisted.artifact_path is None
    assert persisted.artifact_blob is None
    assert persisted.artifact_outcome is None


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ("<voice>  All done.  </voice> details", "All done."),
        ("First sentence. Second sentence.", "First sentence."),
        ("", ""),
    ],
)
def test_extract_voice_summary(result, expected):
    assert extract_voice_summary(result) == expected
