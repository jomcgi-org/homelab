from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from agent_sessions import mcp, store, voice_ui
from agent_sessions.models import (
    AgentSession,
    AgentTurn,
    PendingMessage,
    VoiceUICompanion,
    VoiceUILedger,
)
from agent_sessions.router import router
from auth.dependencies import reset_current_principal, set_current_principal
from auth.principal import Authority, Principal, PrincipalKind


@pytest.fixture(name="engine")
def engine_fixture(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'voice_ui_test.db'}",
        connect_args={"check_same_thread": False},
    )
    table_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            table_schemas[table] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        monkeypatch.setattr(voice_ui, "get_engine", lambda: engine)
        monkeypatch.setattr(mcp, "get_engine", lambda: engine)
        yield engine
    finally:
        for table in SQLModel.metadata.tables.values():
            original_schema = table_schemas.get(table)
            if original_schema is not None:
                table.schema = original_schema


@pytest.fixture(name="session")
def session_fixture(engine):
    with Session(engine) as session:
        yield session


@pytest.fixture(name="client")
def client_fixture(engine):
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client


def _principal() -> Principal:
    return Principal(
        subject="user:test",
        actor=(),
        scope=(),
        groups=(),
        email="test@example.com",
        kind=PrincipalKind.HUMAN,
        authority=Authority.DELEGATED,
    )


def _run_as(principal: Principal, awaitable):
    token = set_current_principal(principal)
    try:
        return asyncio.run(awaitable)
    finally:
        reset_current_principal(token)


def _open_companion() -> str:
    return voice_ui.register_companion(None, "browser:test", "standing")


def _ledger_rows(session: Session) -> list[VoiceUILedger]:
    session.expire_all()
    return list(session.exec(select(VoiceUILedger).order_by(VoiceUILedger.id)).all())


@pytest.mark.parametrize(
    ("tool", "args", "expected"),
    [
        (
            mcp.monolith_voice_ui_attach,
            (),
            {"accepted": True, "companion_open": False},
        ),
        (
            mcp.monolith_voice_ui_show,
            ("run", "run:1"),
            {"accepted": True, "companion_open": False},
        ),
        (
            mcp.monolith_voice_ui_ask,
            ("Continue?", ["Yes", "No"], "ask:1"),
            {"accepted": True, "companion_open": False},
        ),
        (
            mcp.monolith_voice_ui_dismiss,
            (),
            {"accepted": True, "companion_open": False},
        ),
    ],
)
def test_tools_accept_without_open_companion_and_write_nothing(
    session, tool, args, expected
):
    result = asyncio.run(tool(*args))

    assert result == expected
    assert _ledger_rows(session) == []


def test_unknown_surface_is_rejected_identically_with_or_without_companion(session):
    # companion_open is absent on purpose: the call is rejected before any
    # companion lookup, so claiming either value would be a guess.
    expected = {
        "accepted": False,
        "error": (
            "unknown surface unknown; valid surfaces: run, transcript, vm, walkthrough"
        ),
    }

    assert asyncio.run(mcp.monolith_voice_ui_show("unknown", "ref")) == expected
    assert asyncio.run(mcp.monolith_voice_ui_dismiss("unknown")) == expected
    assert _ledger_rows(session) == []

    _open_companion()

    assert asyncio.run(mcp.monolith_voice_ui_show("unknown", "ref")) == expected
    assert asyncio.run(mcp.monolith_voice_ui_dismiss("unknown")) == expected
    assert _ledger_rows(session) == []


@pytest.mark.parametrize("closed", [False, True])
def test_stale_or_closed_companion_is_not_open(session, closed):
    companion_id = _open_companion()
    companion = session.get(VoiceUICompanion, companion_id)
    if closed:
        companion.closed_at = datetime.now(timezone.utc)
    else:
        companion.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=91)
    session.add(companion)
    session.commit()

    result = asyncio.run(mcp.monolith_voice_ui_show("run", "ref"))

    assert result == {"accepted": True, "companion_open": False}
    assert _ledger_rows(session) == []


def test_explicit_attach_binds_session_and_writes_one_row(session):
    companion_id = _open_companion()
    agent_session = store.create_session(session, "existing", "<guest>", "main")

    result = asyncio.run(mcp.monolith_voice_ui_attach(agent_session.id))

    session.expire_all()
    companion = session.get(VoiceUICompanion, companion_id)
    assert result == {
        "accepted": True,
        "session_id": agent_session.id,
        "companion_open": True,
    }
    assert companion.session_id == agent_session.id
    rows = _ledger_rows(session)
    assert len(rows) == 1
    assert rows[0].call == "attach"
    assert rows[0].session_id == agent_session.id


def test_bare_attach_preserves_existing_binding_without_minting(session):
    companion_id = _open_companion()
    agent_session = store.create_session(session, "existing", "<guest>", "main")
    asyncio.run(mcp.monolith_voice_ui_attach(agent_session.id))

    result = asyncio.run(mcp.monolith_voice_ui_attach())

    session.expire_all()
    sessions = list(session.exec(select(AgentSession)).all())
    companion = session.get(VoiceUICompanion, companion_id)
    assert result["session_id"] == agent_session.id
    assert companion.session_id == agent_session.id
    assert [row.id for row in sessions] == [agent_session.id]


def test_bare_attach_mints_zero_turn_session(session, monkeypatch):
    companion_id = _open_companion()

    def unexpected_schedule(_session_id):
        raise AssertionError("voice UI attach must not schedule a message")

    monkeypatch.setattr(mcp, "_schedule_next_message", unexpected_schedule)
    result = asyncio.run(mcp.monolith_voice_ui_attach())

    session.expire_all()
    companion = session.get(VoiceUICompanion, companion_id)
    assert result["accepted"] is True
    assert result["session_id"] == companion.session_id
    assert session.get(AgentSession, result["session_id"]) is not None
    assert list(session.exec(select(AgentTurn)).all()) == []
    assert list(session.exec(select(PendingMessage)).all()) == []


def test_two_bare_attaches_create_one_session(session):
    _open_companion()

    first = asyncio.run(mcp.monolith_voice_ui_attach())
    second = asyncio.run(mcp.monolith_voice_ui_attach())

    session.expire_all()
    assert second["session_id"] == first["session_id"]
    assert len(list(session.exec(select(AgentSession)).all())) == 1
    assert len(_ledger_rows(session)) == 2


def test_second_explicit_attach_rebinds_to_latest_session(session):
    companion_id = _open_companion()
    first = store.create_session(session, "first", "<guest>", "main")
    second = store.create_session(session, "second", "<guest>", "main")

    asyncio.run(mcp.monolith_voice_ui_attach(first.id))
    result = asyncio.run(mcp.monolith_voice_ui_attach(second.id))

    session.expire_all()
    companion = session.get(VoiceUICompanion, companion_id)
    assert result["session_id"] == second.id
    assert companion.session_id == second.id
    assert [row.session_id for row in _ledger_rows(session)] == [first.id, second.id]


def test_unknown_show_surface_is_rejected_without_ledger_row(session):
    _open_companion()

    result = asyncio.run(mcp.monolith_voice_ui_show("map", "ref"))

    assert result == {
        "accepted": False,
        "error": "unknown surface map; valid surfaces: run, transcript, vm, walkthrough",
    }
    assert _ledger_rows(session) == []


@pytest.mark.parametrize("call", ["attach", "show", "ask", "dismiss"])
def test_each_accepted_call_writes_one_row_with_caller_principal(session, call):
    _open_companion()
    principal = _principal()
    if call == "attach":
        agent_session = store.create_session(session, "target", "<guest>", "main")
        result = _run_as(principal, mcp.monolith_voice_ui_attach(agent_session.id))
    elif call == "show":
        result = _run_as(
            principal, mcp.monolith_voice_ui_show("walkthrough", "turn:1", "diff")
        )
    elif call == "ask":
        result = _run_as(
            principal,
            mcp.monolith_voice_ui_ask("Ship it?", ["Yes", "No"], "review:1"),
        )
    else:
        result = _run_as(principal, mcp.monolith_voice_ui_dismiss("transcript"))

    assert result["accepted"] is True
    assert result["companion_open"] is True
    rows = _ledger_rows(session)
    assert len(rows) == 1
    assert rows[0].call == call
    assert rows[0].principal_subject == principal.subject
    assert rows[0].principal_authority == principal.authority


def test_ask_payload_is_recorded_and_returns_immediately(session):
    _open_companion()

    result = asyncio.run(mcp.monolith_voice_ui_ask("Choose", ["A", "B"], "question:1"))

    assert result == {"accepted": True, "companion_open": True}
    assert _ledger_rows(session)[0].payload == {
        "question": "Choose",
        "options": ["A", "B"],
        "ref": "question:1",
    }


def test_registration_with_existing_id_is_heartbeat(client, session):
    first = client.post("/api/agents/companion").json()["companion_id"]
    session.expire_all()
    before = session.get(VoiceUICompanion, first).last_seen_at

    response = client.post("/api/agents/companion", json={"companion_id": first})

    session.expire_all()
    companions = list(session.exec(select(VoiceUICompanion)).all())
    assert response.json() == {"companion_id": first}
    assert len(companions) == 1
    assert isinstance(companions[0].last_seen_at, datetime)
    assert companions[0].last_seen_at >= before


def test_poll_returns_rows_after_since_in_order_and_refreshes_heartbeat(
    client, session
):
    companion_id = client.post("/api/agents/companion").json()["companion_id"]
    voice_ui.show("run", "run:1", None, "caller", "anonymous")
    voice_ui.ask("Continue?", ["Yes"], "ask:1", "caller", "anonymous")
    voice_ui.dismiss(None, "caller", "anonymous")

    rows = _ledger_rows(session)
    old_last_seen = datetime.now(timezone.utc) - timedelta(seconds=10)
    companion = session.get(VoiceUICompanion, companion_id)
    companion.last_seen_at = old_last_seen
    session.add(companion)
    session.commit()

    response = client.get(
        f"/api/agents/companion/{companion_id}/ledger?since={rows[0].id}"
    )

    assert response.status_code == 200
    body = response.json()
    assert [row["id"] for row in body] == [rows[1].id, rows[2].id]
    assert [row["call"] for row in body] == ["ask", "dismiss"]
    session.expire_all()
    refreshed = session.get(VoiceUICompanion, companion_id).last_seen_at
    old_comparable = (
        old_last_seen
        if old_last_seen.tzinfo
        else old_last_seen.replace(tzinfo=timezone.utc)
    )
    refreshed_comparable = (
        refreshed if refreshed.tzinfo else refreshed.replace(tzinfo=timezone.utc)
    )
    assert isinstance(refreshed, datetime)
    assert refreshed_comparable > old_comparable


def test_poll_does_not_refresh_closed_companion_heartbeat(client, session):
    companion_id = client.post("/api/agents/companion").json()["companion_id"]
    companion = session.get(VoiceUICompanion, companion_id)
    companion.last_seen_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    companion.closed_at = datetime.now(timezone.utc)
    session.add(companion)
    session.commit()
    session.expire_all()
    last_seen_at = session.get(VoiceUICompanion, companion_id).last_seen_at

    response = client.get(f"/api/agents/companion/{companion_id}/ledger")

    assert response.status_code == 200
    session.expire_all()
    assert session.get(VoiceUICompanion, companion_id).last_seen_at == last_seen_at
