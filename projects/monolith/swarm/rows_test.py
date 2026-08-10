import json
from datetime import datetime

import pytest
from sqlmodel import Session, SQLModel, create_engine

from agent_sessions.models import AgentSession, AgentTurn, PendingMessage
from swarm.rows import swarm_session_views


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'rows.db'}",
        connect_args={"check_same_thread": False},
    )
    schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            schemas[table.name] = table.schema
            table.schema = None
    SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in schemas:
                table.schema = schemas[table.name]


def add_session(db, workflow_id="wf-1", local_id="local", **kwargs):
    with Session(db) as session:
        row = AgentSession(
            local_session_id=local_id,
            workspace="workspace",
            branch="main",
            workflow_id=workflow_id,
            node_key="implement",
            node_attempt=1,
            **kwargs,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def test_costs_are_grouped_and_zero_without_turns(db):
    first = add_session(db, local_id="first")
    second = add_session(db, local_id="second")
    with Session(db) as session:
        for seq, cost in enumerate((0.1, 0.2, 0.3), 1):
            session.add(
                AgentTurn(
                    session_id=first,
                    seq=seq,
                    prompt="prompt",
                    result_text="result",
                    cost_usd=cost,
                )
            )
        session.commit()
        rows = swarm_session_views(session)["wf-1"]
    by_id = {row["id"]: row for row in rows}
    assert by_id[first]["total_cost_usd"] == pytest.approx(0.6)
    assert by_id[second]["total_cost_usd"] == 0.0
    assert isinstance(by_id[first]["created_at"], datetime)


def test_workflow_filter_and_grouping(db):
    first = add_session(db, "wf-1", "first")
    second = add_session(db, "wf-1", "second")
    other = add_session(db, "wf-2", "other")
    with Session(db) as session:
        filtered = swarm_session_views(session, "wf-1")
        grouped = swarm_session_views(session)
    assert {row["id"] for row in filtered["wf-1"]} == {first, second}
    assert set(grouped) == {"wf-1", "wf-2"}
    assert [row["id"] for row in grouped["wf-2"]] == [other]


@pytest.mark.parametrize(
    ("activity", "expected"),
    [
        ({"type": "edit", "file_path": "a.py"}, "edit a.py"),
        ({"tool": "write", "path": "a.py"}, "write a.py"),
        ({"type": "bash", "command": "git status"}, "run git status"),
        (
            {"name": "grep", "input": {"pattern": "x", "path": "a"}},
            'grep {"pattern":"x","path":"a"}',
        ),
        ("thinking", "thinking"),
        ({"input": "next"}, "step next"),
    ],
)
def test_activity_grammar(db, activity, expected):
    session_id = add_session(db, local_id=expected)
    with Session(db) as session:
        session.add(
            PendingMessage(
                session_id=session_id,
                seq=1,
                message_text="prompt",
                partial_activities=json.dumps([{"type": "old"}, activity]),
                claimed_by_replica="replica-a",
            )
        )
        session.commit()
        row = swarm_session_views(session)["wf-1"][0]
    assert row["activity"] == expected
    assert isinstance(row["activity_observed_at"], datetime)


def test_activity_null_and_malformed_json_are_none(db):
    null_id = add_session(db, local_id="null")
    bad_id = add_session(db, local_id="bad")
    with Session(db) as session:
        session.add_all(
            [
                PendingMessage(session_id=null_id, seq=1, message_text="prompt"),
                PendingMessage(
                    session_id=bad_id,
                    seq=1,
                    message_text="prompt",
                    partial_activities="{not-json",
                ),
            ]
        )
        session.commit()
        rows = swarm_session_views(session)["wf-1"]
    assert {row["local_session_id"]: row["activity"] for row in rows} == {
        "null": None,
        "bad": None,
    }
