import pytest
from sqlmodel import Session, SQLModel, create_engine

import agent_sessions.api as api
import agent_sessions.mcp as mcp
from agent_sessions.models import AgentSession
from agent_sessions.transport import EmberSessionGone
from faas.embervm_client import EmberVMTransportError


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
            "jomcgi-org/homelab",
            "main",
            workflow_id="wf-1",
            node_key="implement",
            node_attempt=2,
        )
        second_id = api.start_session_for_swarm(
            "test-key",
            "prompt1",
            "luna",
            "jomcgi-org/homelab",
            "main",
            workflow_id="wf-2",
        )

        assert second_id == first_id
        with Session(engine) as session:
            row = session.get(AgentSession, first_id)
            assert row is not None
            assert row.workflow_id == "wf-1"
            assert row.node_key == "implement"
            assert row.node_attempt == 2
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in schemas:
                table.schema = schemas[table.name]


@pytest.mark.asyncio
async def test_reap_sessions_for_workflow_skips_reaps_and_continues_on_failure(
    monkeypatch,
):
    rows = [
        AgentSession(id=1, local_session_id="skip", workspace="w", branch="b"),
        AgentSession(
            id=2,
            local_session_id="good",
            workspace="w",
            branch="b",
            ember_session_id="ember-good",
        ),
        AgentSession(
            id=3,
            local_session_id="bad",
            workspace="w",
            branch="b",
            ember_session_id="ember-bad",
        ),
    ]
    destroyed = []
    cleared = []

    async def destroy(ember_id):
        destroyed.append(ember_id)
        if ember_id == "ember-bad":
            raise EmberVMTransportError("control plane unavailable")
        return {}

    monkeypatch.setattr(api, "_sessions_for_workflow", lambda _: rows)
    monkeypatch.setattr(api._transport, "destroy_session", destroy)
    monkeypatch.setattr(
        api, "_clear_ember_bindings_for", lambda ember_id: cleared.append(ember_id)
    )

    assert await api.reap_sessions_for_workflow("wf-1") == {
        "reaped": [2],
        "failed": [{"session_id": 3, "error": "control plane unavailable"}],
        "skipped": [1],
    }
    assert destroyed == ["ember-good", "ember-bad"]
    assert cleared == ["ember-good"]


@pytest.mark.asyncio
async def test_reap_sessions_for_workflow_treats_404_as_reaped(monkeypatch):
    row = AgentSession(
        id=1,
        local_session_id="gone",
        workspace="w",
        branch="b",
        ember_session_id="ember-gone",
    )

    async def destroy(_ember_id):
        raise EmberSessionGone("404 session not found")

    cleared = []
    monkeypatch.setattr(api, "_sessions_for_workflow", lambda _: [row])
    monkeypatch.setattr(api._transport, "destroy_session", destroy)
    monkeypatch.setattr(
        api, "_clear_ember_bindings_for", lambda ember_id: cleared.append(ember_id)
    )

    assert await api.reap_sessions_for_workflow("wf-1") == {
        "reaped": [1],
        "failed": [],
        "skipped": [],
    }
    assert cleared == ["ember-gone"]


@pytest.mark.asyncio
async def test_reap_does_not_treat_a_500_mentioning_404_as_gone(monkeypatch):
    """A live session must never be reported reaped.

    The transport decides "gone" from the STATUS CODE. A plain transport error
    whose text merely contains "404" (the session id itself can, and error
    bodies routinely mention other not-found resources) has to count as a
    FAILURE, otherwise the binding is cleared while the VM keeps burning a
    live-capacity slot with nothing pointing at it.
    """
    row = AgentSession(
        id=7,
        local_session_id="live",
        workspace="w",
        branch="b",
        ember_session_id="s-404ABCDEF",
    )
    cleared = []

    async def destroy(_ember_id):
        raise EmberVMTransportError(
            "Server error '500 Internal Server Error' for url "
            "'http://embervm/v1/sessions/s-404ABCDEF' not found upstream"
        )

    monkeypatch.setattr(api, "_sessions_for_workflow", lambda _: [row])
    monkeypatch.setattr(api._transport, "destroy_session", destroy)
    monkeypatch.setattr(
        api, "_clear_ember_bindings_for", lambda ember_id: cleared.append(ember_id)
    )

    result = await api.reap_sessions_for_workflow("wf-1")

    assert result["reaped"] == []
    assert result["failed"][0]["session_id"] == 7
    assert cleared == []
