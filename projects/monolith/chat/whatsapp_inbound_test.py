"""Tests for the WhatsApp inbound endpoint (ADR 039, spec sections 2/4).

Covers bearer auth (401), registry drop (unknown/disabled group), dedupe (a
replayed message id is a no-op), and attention routing: a directed message
engages and a chat reply lands a whatsapp_outbox row, while an agent-shaped
message gets the honest deferred reply behind the (default-off) flag.

DB-backed via in-memory SQLite (schema stripped for SQLite compat, like
chat.store_test). The concierge reply generator, the depth classifier, and the
attention-decision log are patched so the tests stay hermetic (no LLM, no
embedding service); the attention gate's directed short-circuit is exercised for
real.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from chat import (
    attention,
    attention_log,
    whatsapp_capabilities,
    whatsapp_inbound,
    whatsapp_session,
)
from chat.attention import AttentionResult
from chat.models import WhatsappGroup, WhatsappOutbox

_TOKEN = "s3cret-token"
_GROUP = "12345-67890@g.us"


@pytest.fixture
def engine():
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
    SQLModel.metadata.create_all(engine)
    yield engine
    for table in SQLModel.metadata.tables.values():
        if table.name in original_schemas:
            table.schema = original_schemas[table.name]


@pytest.fixture
def client(engine, monkeypatch):
    """A TestClient over just the inbound router, with the DB, embeddings, the
    reply generator, the depth classifier, and the decision log patched."""
    monkeypatch.setenv("WHATSAPP_INBOUND_TOKEN", _TOKEN)

    # Both the handler and _lookup_enabled_group read get_engine from the module.
    monkeypatch.setattr(whatsapp_inbound, "get_engine", lambda: engine)
    # The session-keyed helpers (steer_or_none, dispatch) open their own sessions
    # via chat.whatsapp_session.get_engine; point it at the same test engine so
    # the real steer_or_none (unpatched in the chat/deferred tests) sees no
    # session row and returns False rather than hitting a real DB.
    monkeypatch.setattr(whatsapp_session, "get_engine", lambda: engine)
    # The household capability router (run before the depth split) opens its own
    # sessions to check for a pending action; point it at the test engine so an
    # engaged, keyword-free message resolves to "no capability" against SQLite
    # rather than the real DB.
    monkeypatch.setattr(whatsapp_capabilities, "get_engine", lambda: engine)

    fake_embed = AsyncMock()
    fake_embed.embed_batch.return_value = [[0.0] * 1024]
    monkeypatch.setattr(whatsapp_inbound, "_embed_client", lambda: fake_embed)

    # Decision logging opens its own session; on SQLite StaticPool that would
    # nest on the handler's open transaction, so stub it (its behaviour is tested
    # in attention_log_test).
    monkeypatch.setattr(attention_log, "log_decision", lambda *a, **k: None)

    app = FastAPI()
    app.include_router(whatsapp_inbound.router)
    return TestClient(app)


def _seed_group(engine, *, enabled=True, ambient=True):
    with Session(engine) as session:
        session.add(WhatsappGroup(group_jid=_GROUP, enabled=enabled, ambient=ambient))
        session.commit()


def _post(client, *, token=_TOKEN, text="hello", message_id="M1", **overrides):
    body = {
        "group_jid": _GROUP,
        "sender_jid": "alice@s.whatsapp.net",
        "sender_name": "Alice",
        "message_id": message_id,
        "text": text,
        "timestamp": "2026-07-03T10:00:00Z",
    }
    body.update(overrides)
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/internal/whatsapp/inbound", json=body, headers=headers)


def _outbox(engine):
    with Session(engine) as session:
        return session.exec(select(WhatsappOutbox)).all()


class TestAuth:
    def test_missing_bearer_is_401(self, client, engine):
        _seed_group(engine)
        resp = _post(client, token=None)
        assert resp.status_code == 401

    def test_wrong_bearer_is_401(self, client, engine):
        _seed_group(engine)
        resp = _post(client, token="nope")
        assert resp.status_code == 401
        assert _outbox(engine) == []


class TestRegistryDrop:
    def test_unknown_group_is_dropped(self, client, engine):
        # No group seeded.
        resp = _post(client)
        assert resp.status_code == 200
        assert resp.json()["status"] == "dropped"
        assert _outbox(engine) == []

    def test_disabled_group_is_dropped(self, client, engine):
        _seed_group(engine, enabled=False)
        resp = _post(client)
        assert resp.json()["status"] == "dropped"
        assert _outbox(engine) == []


class TestDedupe:
    def test_replayed_message_id_is_a_noop(self, client, engine, monkeypatch):
        _seed_group(engine)
        # Non-directed, ambient-ignore so no reply work happens; we only assert
        # the second delivery of the same id is deduped.
        monkeypatch.setattr(
            attention, "evaluate", AsyncMock(return_value=AttentionResult(False, 0.0))
        )
        first = _post(client, text="just chatter", message_id="DUP")
        assert first.json()["status"] == "ignored"
        second = _post(client, text="just chatter", message_id="DUP")
        assert second.json()["status"] == "duplicate"


class TestAttentionRouting:
    def test_directed_chat_enqueues_reply(self, client, engine, monkeypatch):
        _seed_group(engine)
        # Depth says chat; the reply generator is stubbed to a known string.
        monkeypatch.setattr(attention, "needs_agent", AsyncMock(return_value=False))
        monkeypatch.setattr(
            whatsapp_inbound,
            "_generate_reply",
            AsyncMock(return_value="the ferry leaves at 9"),
        )
        # "bosun" is the default trigger name, so this is directed (no classifier).
        resp = _post(client, text="hey bosun what's the ferry plan?", message_id="Q1")
        assert resp.json()["status"] == "replied"
        rows = _outbox(engine)
        assert len(rows) == 1
        assert rows[0].kind == "message"
        assert rows[0].group_jid == _GROUP
        assert rows[0].content == "the ferry leaves at 9"
        # The reply quotes the triggering message.
        assert rows[0].quoted_message_id == "Q1"

    def test_directed_agent_gets_honest_deferred_reply(
        self, client, engine, monkeypatch
    ):
        _seed_group(engine)
        # Depth says agent; Phase 3 defers dispatch, so an honest one-liner is
        # enqueued instead (flag default-off).
        monkeypatch.setattr(attention, "needs_agent", AsyncMock(return_value=True))
        gen = AsyncMock(return_value="should not be called")
        monkeypatch.setattr(whatsapp_inbound, "_generate_reply", gen)
        resp = _post(client, text="bosun build me a dashboard", message_id="A1")
        assert resp.json()["status"] == "replied"
        rows = _outbox(engine)
        assert len(rows) == 1
        assert rows[0].content == whatsapp_inbound._AGENT_DEFERRED_REPLY
        gen.assert_not_called()

    def test_ambient_ignore_enqueues_nothing(self, client, engine, monkeypatch):
        _seed_group(engine)
        monkeypatch.setattr(
            attention, "evaluate", AsyncMock(return_value=AttentionResult(False, 0.1))
        )
        resp = _post(client, text="just the two of us talking", message_id="I1")
        assert resp.json()["status"] == "ignored"
        assert _outbox(engine) == []


class TestAgentDispatch:
    """Phase 4: engaged agent-shaped messages steer a live run or dispatch one."""

    def test_running_session_steers_not_dispatches(self, client, engine, monkeypatch):
        _seed_group(engine)
        monkeypatch.setattr(attention, "needs_agent", AsyncMock(return_value=True))
        # A live run swallows the message as steering (real dispatch never called).
        monkeypatch.setattr(whatsapp_session, "steer_or_none", lambda *a, **k: True)
        called = []
        monkeypatch.setattr(
            whatsapp_session,
            "dispatch_whatsapp_agent",
            lambda *a, **k: called.append(k) or {"action": "dispatched"},
        )
        resp = _post(client, text="bosun tweak that", message_id="S1")
        assert resp.json()["status"] == "steering"
        assert called == []

    def test_agent_dispatch_when_enabled(self, client, engine, monkeypatch):
        _seed_group(engine)
        monkeypatch.setattr(whatsapp_inbound, "_AGENT_ENABLED", True)
        monkeypatch.setattr(attention, "needs_agent", AsyncMock(return_value=True))
        monkeypatch.setattr(whatsapp_session, "steer_or_none", lambda *a, **k: False)
        seen = {}

        def _fake_dispatch(
            group_jid, prompt, *, trigger_message_id, trigger_sender_jid, repo=""
        ):
            seen.update(
                group_jid=group_jid,
                prompt=prompt,
                message_id=trigger_message_id,
                sender_jid=trigger_sender_jid,
            )
            return {"action": "dispatched"}

        monkeypatch.setattr(whatsapp_session, "dispatch_whatsapp_agent", _fake_dispatch)
        resp = _post(
            client,
            text="bosun build me a plan",
            message_id="A2",
            sender_jid="alice@s.whatsapp.net",
        )
        assert resp.json()["status"] == "dispatched"
        assert seen["group_jid"] == _GROUP
        assert seen["message_id"] == "A2"
        assert seen["sender_jid"] == "alice@s.whatsapp.net"
        assert seen["prompt"] == "bosun build me a plan"

    def test_agent_disabled_gets_deferred_reply(self, client, engine, monkeypatch):
        _seed_group(engine)
        # Flag default-off: no dispatch, honest one-liner instead.
        monkeypatch.setattr(attention, "needs_agent", AsyncMock(return_value=True))
        monkeypatch.setattr(whatsapp_session, "steer_or_none", lambda *a, **k: False)
        called = []
        monkeypatch.setattr(
            whatsapp_session,
            "dispatch_whatsapp_agent",
            lambda *a, **k: called.append(k) or {"action": "dispatched"},
        )
        resp = _post(client, text="bosun ship the feature", message_id="A3")
        assert resp.json()["status"] == "replied"
        assert called == []
        rows = _outbox(engine)
        assert len(rows) == 1
        assert rows[0].content == whatsapp_inbound._AGENT_DEFERRED_REPLY
