from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from agent_sessions import store
from agent_sessions.voice import extract_voice_summary


@pytest.fixture
def session():
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
