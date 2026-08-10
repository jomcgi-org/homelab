from sqlmodel import Session, SQLModel, create_engine

import agent_sessions.api as api
import agent_sessions.mcp as mcp
from agent_sessions.models import AgentSession


def test_start_session_for_swarm_retry_preserves_original_workflow_id(
    monkeypatch, tmp_path
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'api_test.db'}",
        connect_args={"check_same_thread": False},
    )
    schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            schemas[table.name] = table.schema
            table.schema = None
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(api, "get_engine", lambda: engine)
    monkeypatch.setattr(mcp, "get_engine", lambda: engine)
    monkeypatch.setattr(api, "_schedule_next_message", lambda session_id: None)

    try:
        first_id = api.start_session_for_swarm(
            "test-key",
            "prompt1",
            "luna",
            "jomcgi/homelab",
            "main",
            workflow_id="wf-1",
        )
        second_id = api.start_session_for_swarm(
            "test-key",
            "prompt1",
            "luna",
            "jomcgi/homelab",
            "main",
            workflow_id="wf-2",
        )

        assert second_id == first_id
        with Session(engine) as session:
            row = session.get(AgentSession, first_id)
            assert row is not None
            assert row.workflow_id == "wf-1"
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in schemas:
                table.schema = schemas[table.name]
