"""Tests for the WhatsApp inbound endpoint (ADR 039, spec sections 2/4).

Covers bearer auth (401), registry drop (unknown/disabled group), dedupe (a
replayed message id is a no-op), and attention routing: a directed message
engages and a chat reply lands a whatsapp_outbox row, while an agent-shaped
message gets the honest unavailable reply.

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
from chat.models import ReactionEvent, WhatsappGroup, WhatsappOutbox

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
    # The session-keyed steering helper opens its own session via
    # chat.whatsapp_session.get_engine; point it at the same test engine so the
    # real steer_or_none sees no session row and returns False rather than hitting
    # a real DB.
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
        # Fast-ack: the reply is authored in a background task, so the request
        # returns "accepted" while the reply lands via the outbox.
        assert resp.json()["status"] == "accepted"
        rows = _outbox(engine)
        # The ⏳ working reaction, the reply message, then the ⏳ clear.
        msgs = [r for r in rows if r.kind == "message"]
        assert len(msgs) == 1
        assert msgs[0].group_jid == _GROUP
        assert msgs[0].content == "the ferry leaves at 9"
        # The reply quotes the triggering message.
        assert msgs[0].quoted_message_id == "Q1"
        # An instant working reaction was enqueued on the triggering message.
        assert any(r.kind == "reaction" and r.target_message_id == "Q1" for r in rows)

    def test_directed_agent_gets_honest_deferred_reply(
        self, client, engine, monkeypatch
    ):
        _seed_group(engine)
        # Depth says agent, so an honest unavailable one-liner is enqueued.
        monkeypatch.setattr(attention, "needs_agent", AsyncMock(return_value=True))
        gen = AsyncMock(return_value="should not be called")
        monkeypatch.setattr(whatsapp_inbound, "_generate_reply", gen)
        resp = _post(client, text="bosun build me a dashboard", message_id="A1")
        assert resp.json()["status"] == "replied"
        rows = _outbox(engine)
        assert len(rows) == 1
        assert rows[0].content == whatsapp_inbound._AGENT_DEFERRED_REPLY
        gen.assert_not_called()

    def test_reply_to_bot_sent_id_engages_in_non_ambient_group(
        self, client, engine, monkeypatch
    ):
        # A non-ambient group: only a directed message engages. The bot's real
        # sent id lives on the outbox row (sent_message_id), never in the messages
        # table, so a reply quoting it must be resolved against the outbox.
        _seed_group(engine, ambient=False)
        with Session(engine) as session:
            session.add(
                WhatsappOutbox(
                    group_jid=_GROUP,
                    kind="message",
                    content="earlier bot reply",
                    sent_message_id="wamid.BOTSENT",
                )
            )
            session.commit()
        monkeypatch.setattr(attention, "needs_agent", AsyncMock(return_value=False))
        monkeypatch.setattr(
            whatsapp_inbound,
            "_generate_reply",
            AsyncMock(return_value="you asked about the ferry"),
        )
        # No trigger word; directedness comes solely from quoting the bot's send.
        resp = _post(
            client,
            text="and what time does it get back?",
            message_id="R1",
            quoted_message_id="wamid.BOTSENT",
        )
        assert resp.json()["status"] == "accepted"
        replies = [
            r for r in _outbox(engine) if r.content == "you asked about the ferry"
        ]
        assert len(replies) == 1
        assert replies[0].quoted_message_id == "R1"

    def test_ambient_ignore_enqueues_nothing(self, client, engine, monkeypatch):
        _seed_group(engine)
        monkeypatch.setattr(
            attention, "evaluate", AsyncMock(return_value=AttentionResult(False, 0.1))
        )
        resp = _post(client, text="just the two of us talking", message_id="I1")
        assert resp.json()["status"] == "ignored"
        assert _outbox(engine) == []


class TestAgentHandling:
    """Agent-shaped messages steer a live run or receive an unavailable reply."""

    def test_running_session_steers_not_dispatches(self, client, engine, monkeypatch):
        _seed_group(engine)
        monkeypatch.setattr(attention, "needs_agent", AsyncMock(return_value=True))
        # A live run swallows the message as steering.
        monkeypatch.setattr(whatsapp_session, "steer_or_none", lambda *a, **k: True)
        resp = _post(client, text="bosun tweak that", message_id="S1")
        assert resp.json()["status"] == "steering"

    def test_agent_gets_unavailable_reply_without_live_session(
        self, client, engine, monkeypatch
    ):
        _seed_group(engine)
        monkeypatch.setattr(attention, "needs_agent", AsyncMock(return_value=True))
        monkeypatch.setattr(whatsapp_session, "steer_or_none", lambda *a, **k: False)
        resp = _post(client, text="bosun ship the feature", message_id="A3")
        assert resp.json()["status"] == "replied"
        rows = _outbox(engine)
        assert len(rows) == 1
        assert rows[0].content == whatsapp_inbound._AGENT_DEFERRED_REPLY


def _seed_bot_sent(engine, sent_message_id="wamid.BOTSENT"):
    """Record a message the bot actually sent (a reactable bot target)."""
    with Session(engine) as session:
        session.add(
            WhatsappOutbox(
                group_jid=_GROUP,
                kind="message",
                content="earlier bot reply",
                sent_message_id=sent_message_id,
            )
        )
        session.commit()


def _post_reaction(client, *, token=_TOKEN, **overrides):
    body = {
        "group_jid": _GROUP,
        "reactor_jid": "alice@s.whatsapp.net",
        "target_message_id": "wamid.BOTSENT",
        "emoji": "👍",
        "timestamp": "2026-07-03T10:00:00Z",
    }
    body.update(overrides)
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/internal/whatsapp/reaction", json=body, headers=headers)


def _reactions(engine):
    with Session(engine) as session:
        return session.exec(select(ReactionEvent).order_by(ReactionEvent.id)).all()


class TestReaction:
    """The /reaction endpoint: the WhatsApp half of the /improve-ambient signal."""

    def test_missing_bearer_is_401(self, client, engine):
        _seed_group(engine)
        assert _post_reaction(client, token=None).status_code == 401
        assert _reactions(engine) == []

    def test_unknown_group_is_dropped(self, client, engine):
        # No group seeded.
        resp = _post_reaction(client)
        assert resp.status_code == 200
        assert resp.json()["status"] == "dropped"
        assert _reactions(engine) == []

    def test_reaction_on_non_bot_message_is_dropped(self, client, engine):
        # No outbox row for the target: it was not a message the bot sent, so it
        # is not a signal about Bosun (defense in depth behind the gateway gate).
        _seed_group(engine)
        resp = _post_reaction(client, target_message_id="wamid.HUMAN")
        assert resp.json()["status"] == "dropped"
        assert _reactions(engine) == []

    def test_add_on_bot_message_is_recorded(self, client, engine):
        _seed_group(engine)
        _seed_bot_sent(engine)
        resp = _post_reaction(client, emoji="👍")
        assert resp.json()["status"] == "recorded"
        rows = _reactions(engine)
        assert len(rows) == 1
        assert rows[0].channel_id == _GROUP
        assert rows[0].message_id == "wamid.BOTSENT"
        assert rows[0].reactor_id == "alice@s.whatsapp.net"
        assert rows[0].emoji == "👍"
        assert rows[0].action == "add"
        assert rows[0].target_is_bot is True

    def test_duplicate_add_is_a_noop(self, client, engine):
        # The gateway is at-least-once; a replayed identical add must not double
        # the signal.
        _seed_group(engine)
        _seed_bot_sent(engine)
        assert _post_reaction(client, emoji="👍").json()["status"] == "recorded"
        assert _post_reaction(client, emoji="👍").json()["status"] == "dropped"
        assert len(_reactions(engine)) == 1

    def test_removal_cancels_the_add(self, client, engine):
        # WhatsApp sends an empty reaction to remove; it cancels whatever the
        # reactor's current reaction was (an add + a remove of the same emoji).
        _seed_group(engine)
        _seed_bot_sent(engine)
        _post_reaction(client, emoji="👍")
        resp = _post_reaction(client, emoji="")
        assert resp.json()["status"] == "recorded"
        rows = _reactions(engine)
        assert [r.action for r in rows] == ["add", "remove"]
        # The remove carries the cancelled emoji so valence scoring negates it.
        assert rows[1].emoji == "👍"

    def test_removal_with_no_active_reaction_is_dropped(self, client, engine):
        _seed_group(engine)
        _seed_bot_sent(engine)
        resp = _post_reaction(client, emoji="")
        assert resp.json()["status"] == "dropped"
        assert _reactions(engine) == []

    def test_replace_cancels_old_and_adds_new(self, client, engine):
        # Changing 👍 to ❤️ (WhatsApp's one-reaction-per-message replace) nets to
        # just ❤️: a remove of 👍 then an add of ❤️.
        _seed_group(engine)
        _seed_bot_sent(engine)
        _post_reaction(client, emoji="👍")
        resp = _post_reaction(client, emoji="❤️")
        assert resp.json()["status"] == "recorded"
        rows = _reactions(engine)
        assert [(r.action, r.emoji) for r in rows] == [
            ("add", "👍"),
            ("remove", "👍"),
            ("add", "❤️"),
        ]
        # The reactor's current reaction is now ❤️.
        with Session(engine) as session:
            assert (
                whatsapp_inbound._current_reaction(
                    session, _GROUP, "wamid.BOTSENT", "alice@s.whatsapp.net"
                )
                == "❤️"
            )
