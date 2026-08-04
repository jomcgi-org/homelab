from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from agent_sessions import store
from agent_sessions import mcp
from agent_sessions.models import AgentSession, AgentTurn, PendingMessage
from agent_sessions.router import router
from core.db import get_session


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            yield session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


@pytest.fixture(name="client")
def client_fixture(session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _session(session: Session, name: str, status: str = "running", **kwargs):
    row = AgentSession(
        local_session_id=name,
        workspace=kwargs.pop("workspace", "<guest>"),
        branch=kwargs.pop("branch", "main"),
        status=status,
        **kwargs,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def test_list_sessions_empty(client):
    response = client.get("/api/agents/sessions")
    assert response.status_code == 200
    assert response.json() == []


def test_list_sessions_with_status_filter(client, session):
    _session(session, "running", "running")
    _session(session, "done", "completed")

    body = client.get("/api/agents/sessions?status=completed").json()
    assert [item["local_session_id"] for item in body] == ["done"]


def test_list_sessions_ordering(client, session):
    # A NULL last_turn_at is unrepresentable: the column is NOT NULL DEFAULT
    # NOW() in Postgres, and the model's default_factory backfills an explicit
    # None at INSERT time, so only the DESC ordering is observable.
    now = datetime.now(timezone.utc)
    _session(session, "oldest", last_turn_at=now - timedelta(minutes=5))
    _session(session, "old", last_turn_at=now - timedelta(minutes=1))
    _session(session, "new", last_turn_at=now)

    body = client.get("/api/agents/sessions").json()
    assert [item["local_session_id"] for item in body] == ["new", "old", "oldest"]


def test_list_sessions_aggregates(client, session):
    row = _session(session, "aggregate")
    session.add_all(
        [
            AgentTurn(
                session_id=row.id,
                seq=1,
                prompt="one",
                result_text="done",
                cost_usd=0.06,
            ),
            AgentTurn(
                session_id=row.id,
                seq=2,
                prompt="two",
                result_text="done",
                cost_usd=0.04,
            ),
            PendingMessage(session_id=row.id, seq=3, message_text="three"),
        ]
    )
    session.commit()

    item = client.get("/api/agents/sessions").json()[0]
    assert item["turn_count"] == 2
    assert item["pending_count"] == 1
    assert item["total_cost_usd"] == pytest.approx(0.1)


def test_get_session_detail(client, session):
    row = _session(session, "detail")
    session.add_all(
        [
            AgentTurn(
                session_id=row.id,
                seq=2,
                prompt="two",
                result_text="result",
                usage_json='{"activities": ["shell"]}',
            ),
            AgentTurn(session_id=row.id, seq=1, prompt="one", result_text="result"),
            PendingMessage(session_id=row.id, seq=3, message_text="next"),
        ]
    )
    session.commit()

    body = client.get(f"/api/agents/sessions/{row.id}").json()
    assert body["session"]["id"] == row.id
    assert [turn["seq"] for turn in body["turns"]] == [1, 2]
    assert body["turns"][1]["usage"] == {"activities": ["shell"]}
    assert body["pending_queue"][0]["prompt"] == "next"


def test_get_session_not_found(client):
    assert client.get("/api/agents/sessions/999").status_code == 404


def test_start_session_happy_path(client, session, monkeypatch):
    monkeypatch.setattr(
        "agent_sessions.router._persist_session",
        lambda local, workspace, branch, model: store.create_session(
            session, local, workspace, branch, model
        ),
    )
    monkeypatch.setattr(
        "agent_sessions.router._persist_pending_message",
        lambda session_id, prompt, model: (
            store.create_pending_message(session, session_id, prompt, model).seq
        ),
    )
    monkeypatch.setattr("agent_sessions.router._schedule_next_message", lambda _: None)

    body = client.post("/api/agents/sessions", json={"prompt": "Hello"}).json()
    assert body["accepted"] is True
    assert body["turn"] == 1


def test_start_session_model_validation(client):
    body = client.post(
        "/api/agents/sessions", json={"prompt": "Hello", "model": "unknown"}
    ).json()
    assert body["accepted"] is False
    assert "Unknown model" in body["error"]


def test_send_message_happy_path(client, session, monkeypatch):
    row = _session(session, "send", status="completed")
    monkeypatch.setattr("agent_sessions.router._load_session_row", lambda _: row)
    monkeypatch.setattr(
        "agent_sessions.router._persist_pending_message",
        lambda session_id, prompt, model: (
            store.create_pending_message(session, session_id, prompt, model).seq
        ),
    )
    monkeypatch.setattr(
        "agent_sessions.router._set_session_status",
        lambda session_id, status: store.update_session_status(
            session, session_id, status
        ),
    )
    monkeypatch.setattr("agent_sessions.router._schedule_next_message", lambda _: None)

    body = client.post(
        f"/api/agents/sessions/{row.id}/messages", json={"prompt": "follow up"}
    ).json()
    assert body == {"accepted": True, "session_id": row.id, "turn": 1}
    assert session.get(AgentSession, row.id).status == "running"


def test_send_message_model_family_mismatch(client, session, monkeypatch):
    row = _session(session, "pinned", model="opus")
    monkeypatch.setattr("agent_sessions.router._load_session_row", lambda _: row)
    body = client.post(
        f"/api/agents/sessions/{row.id}/messages",
        json={"prompt": "follow up", "model": "luna"},
    ).json()
    assert body["accepted"] is False
    assert "Model family mismatch" in body["error"]


def test_send_message_session_not_found(client, monkeypatch):
    monkeypatch.setattr("agent_sessions.router._load_session_row", lambda _: None)
    body = client.post(
        "/api/agents/sessions/999/messages", json={"prompt": "hello"}
    ).json()
    assert body == {"accepted": False, "error": "Unknown agent session 999"}


def test_delete_session(client, session, monkeypatch):
    row = _session(session, "delete", ember_session_id="ember-1")
    monkeypatch.setattr(
        "agent_sessions.router._clear_ember_bindings_for_session",
        lambda session_id: store.clear_ember_session(session, session_id),
    )
    assert client.delete(f"/api/agents/sessions/{row.id}").json() == {}
    assert session.get(AgentSession, row.id).ember_session_id is None


def test_search_empty_query(client):
    assert client.get("/api/agents/search?q=").json() == {"results": []}


def test_mcp_tools_still_work(session, monkeypatch):
    monkeypatch.setattr(
        mcp,
        "_persist_session",
        lambda local, workspace, branch, model: store.create_session(
            session, local, workspace, branch, model
        ),
    )
    monkeypatch.setattr(
        mcp,
        "_persist_pending_message",
        lambda session_id, prompt, model: (
            store.create_pending_message(session, session_id, prompt, model).seq
        ),
    )
    monkeypatch.setattr(mcp, "_schedule_next_message", lambda _: None)

    body = asyncio.run(mcp.monolith_agent_session_start("hello"))
    assert body["accepted"] is True
    assert body["turn"] == 1
