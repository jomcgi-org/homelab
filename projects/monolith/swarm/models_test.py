from datetime import datetime
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from swarm.models import (
    SwarmConductorCall,
    SwarmPlanNode,
    SwarmPlanVersion,
    SwarmTask,
    append_plan_version,
    create_task,
    mint_task_id,
    record_conductor_call,
    upsert_plan_node,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'swarm.db'}")
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


def test_mint_task_id_is_prefixed_uuid():
    task_id = mint_task_id()
    assert task_id.startswith("t-")
    UUID(task_id.removeprefix("t-"))


def test_create_task_writes_row(db):
    task_id = mint_task_id()
    with Session(db) as session:
        row = create_task(
            task_id,
            "build the thing",
            "homelab",
            "main",
            "qwen",
            2.5,
            workflow_id="wf-1",
            session_id=7,
            session=session,
        )

    with Session(db) as session:
        stored = session.get(SwarmTask, task_id)
        assert stored is not None
        assert stored.task_text == row.task_text == "build the thing"
        assert stored.workflow_id == "wf-1"
        assert stored.session_id == 7
        assert isinstance(stored.created_at, datetime)


def test_append_plan_version_rejects_duplicate_version(db):
    task_id = mint_task_id()
    with Session(db) as session:
        create_task(task_id, "task", None, None, "model", None, session=session)
        append_plan_version(
            task_id, 1, "init", "system", "model", "{}", "user_message", session=session
        )

    with pytest.raises(IntegrityError):
        with Session(db) as session:
            append_plan_version(
                task_id,
                1,
                "add_node",
                "conductor",
                "model",
                "{}",
                "condition",
                session=session,
            )


def test_upsert_plan_node_inserts_and_updates(db):
    task_id = mint_task_id()
    with Session(db) as session:
        create_task(task_id, "task", None, None, "model", None, session=session)
        inserted = upsert_plan_node(
            task_id,
            "node-1",
            "converse",
            "say hello",
            "model",
            "[]",
            0.5,
            False,
            2,
            60,
            1,
            session=session,
        )
        updated = upsert_plan_node(
            task_id,
            "node-1",
            "delivery",
            "say goodbye",
            None,
            '["node-0"]',
            1.0,
            True,
            3,
            120,
            2,
            session=session,
        )

    assert inserted.id == updated.id
    with Session(db) as session:
        stored = session.exec(
            select(SwarmPlanNode).where(SwarmPlanNode.task_id == task_id)
        ).one()
        assert stored.kind == "delivery"
        assert stored.prompt == "say goodbye"
        assert stored.deps_json == '["node-0"]'


def test_record_conductor_call_writes_all_fields(db):
    task_id = mint_task_id()
    with Session(db) as session:
        create_task(task_id, "task", None, None, "model", None, session=session)
        row = record_conductor_call(
            task_id,
            "qwen",
            "swarm_add_node",
            '{"node_key":"node-1"}',
            "refused",
            refusal_code="budget_exceeded",
            version_before=2,
            version_after=None,
            latency_ms=123,
            session=session,
        )

    with Session(db) as session:
        stored = session.get(SwarmConductorCall, row.id)
        assert stored is not None
        assert stored.task_id == task_id
        assert stored.conductor_model == "qwen"
        assert stored.tool == "swarm_add_node"
        assert stored.args_json == '{"node_key":"node-1"}'
        assert stored.outcome == "refused"
        assert stored.refusal_code == "budget_exceeded"
        assert stored.version_before == 2
        assert stored.version_after is None
        assert stored.latency_ms == 123
        assert isinstance(stored.created_at, datetime)
