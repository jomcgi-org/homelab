"""Tests for chat.outbox: enqueue validation and the drain posting logic."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

import chat.directives as directives
import chat.outbox as outbox
from chat.models import DiscordOutbox
from chat.outbox import drain_once, enqueue_message


def _directive_proposal_row(row_id: int = 8) -> dict:
    """A pending directive-proposal outbox row as _claim_pending would hand it
    to the drain (a plain content post carrying kind/payload_json)."""
    return {
        "id": row_id,
        "channel_id": "100",
        "content": "Proposed directive for this channel:\n> reply concisely",
        "embed_json": None,
        "level": "info",
        "target_message_id": None,
        "reaction": None,
        "reaction_remove": False,
        "kind": "directive_proposal",
        "payload_json": json.dumps(
            {
                "channel_id": "100",
                "directive_change": "reply concisely",
                "evidence_message_ids": ["1", "2", "3"],
                "motivating_message_id": "1",
            }
        ),
    }


def _posted_message() -> MagicMock:
    message = MagicMock()
    message.id = 999
    message.add_reaction = AsyncMock()
    message.edit = AsyncMock()
    return message


@pytest.fixture(name="engine")
def engine_fixture():
    """In-memory SQLite engine with the chat schema stripped, so the DB-level
    CHECK constraint (mirrored into the model) is actually exercised."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original[table.name] = table.schema
            table.schema = None
    SQLModel.metadata.create_all(engine)
    yield engine
    for table in SQLModel.metadata.tables.values():
        if table.name in original:
            table.schema = original[table.name]


def test_reaction_row_satisfies_content_or_embed_check(engine):
    """A reaction row (no content, no embed) must be accepted by the DB CHECK -
    the regression that wedged prod: the constraint required content OR embed."""
    with Session(engine) as session:
        session.add(
            DiscordOutbox(
                channel_id="chan",
                target_message_id="msg",
                reaction="⏳",
                reaction_remove=True,
            )
        )
        session.commit()
    with Session(engine) as session:
        row = session.query(DiscordOutbox).one()
    assert row.reaction == "⏳" and row.content is None and row.embed_json is None


def test_fully_empty_outbox_row_is_rejected(engine):
    """The relaxed CHECK still rejects a row with no content, embed, or reaction."""
    with Session(engine) as session:
        session.add(
            DiscordOutbox(
                channel_id="chan",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_enqueue_text_adds_row():
    session = MagicMock()
    enqueue_message(session, "C1", content="hello", level="warn")
    session.add.assert_called_once()
    row = session.add.call_args.args[0]
    assert row.channel_id == "C1"
    assert row.content == "hello"
    assert row.embed_json is None
    assert row.level == "warn"


def test_enqueue_embed_serialises_json():
    session = MagicMock()
    enqueue_message(session, "C1", embed={"title": "T"})
    row = session.add.call_args.args[0]
    assert row.content is None
    assert row.embed_json == '{"title": "T"}'


def test_enqueue_requires_exactly_one_of_content_embed():
    session = MagicMock()
    with pytest.raises(ValueError, match="exactly one"):
        enqueue_message(session, "C1")
    with pytest.raises(ValueError, match="exactly one"):
        enqueue_message(session, "C1", content="x", embed={"a": 1})


@pytest.mark.asyncio
async def test_drain_posts_text_and_marks_posted():
    row = {
        "id": 1,
        "channel_id": "100",
        "content": "ping",
        "embed_json": None,
        "level": "info",
    }
    channel = MagicMock()
    channel.send = AsyncMock()
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)

    with (
        patch.object(outbox, "_claim_pending", return_value=[row]),
        patch.object(outbox, "_mark_posted") as mark_posted,
        patch.object(outbox, "_mark_failed") as mark_failed,
    ):
        posted = await drain_once(bot, engine=object())

    channel.send.assert_awaited_once_with("ping")
    mark_posted.assert_called_once()
    mark_failed.assert_not_called()
    assert posted == 1


@pytest.mark.asyncio
async def test_drain_posts_embed():
    row = {
        "id": 2,
        "channel_id": "100",
        "content": None,
        "embed_json": '{"title": "Changelog"}',
        "level": "info",
    }
    channel = MagicMock()
    channel.send = AsyncMock()
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)

    with (
        patch.object(outbox, "_claim_pending", return_value=[row]),
        patch.object(outbox, "_mark_posted"),
        patch.object(outbox, "_mark_failed"),
    ):
        await drain_once(bot, engine=object())

    # Posted as an embed, not text.
    assert channel.send.await_args.kwargs.get("embed") is not None


def test_edit_row_satisfies_content_or_embed_check(engine):
    """An edit row (content + target_message_id, no reaction) is a valid outbox
    entry: content is non-NULL, so it passes the content-or-embed-or-reaction
    CHECK. Legacy columns remain until a separate live-row audit and migration."""
    with Session(engine) as session:
        session.add(
            DiscordOutbox(
                channel_id="chan",
                target_message_id="msg",
                content="final result",
            )
        )
        session.commit()
    with Session(engine) as session:
        row = session.query(DiscordOutbox).one()
    assert row.target_message_id == "msg" and row.content == "final result"
    assert row.reaction is None


def test_enqueue_message_kind_and_payload_round_trip(engine):
    """A directive-proposal row persists kind and its JSON-serialised payload;
    a plain post leaves both at their empty defaults."""
    payload = {"channel_id": "100", "directive_change": "reply concisely"}
    with Session(engine) as session:
        enqueue_message(
            session,
            "100",
            content="proposal",
            kind="directive_proposal",
            payload=payload,
        )
        enqueue_message(session, "100", content="plain")
        session.commit()
    with Session(engine) as session:
        rows = session.query(DiscordOutbox).order_by(DiscordOutbox.id).all()
    tagged, plain = rows
    assert tagged.kind == "directive_proposal"
    assert json.loads(tagged.payload_json) == payload
    assert plain.kind == "" and plain.payload_json is None


@pytest.mark.asyncio
async def test_drain_directive_proposal_stages_and_reacts():
    """A posted directive-proposal row stages the proposal keyed on the posted
    message id (user_id 'observer') and seeds 👍/👎 when the guard accepts it."""
    row = _directive_proposal_row()
    message = _posted_message()
    channel = MagicMock()
    channel.send = AsyncMock(return_value=message)
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)

    with (
        patch.object(outbox, "_claim_pending", return_value=[row]),
        patch.object(outbox, "_mark_posted") as mark_posted,
        patch.object(outbox, "_mark_failed") as mark_failed,
        patch.object(directives, "propose_update", return_value=(True, "")) as prop,
    ):
        posted = await drain_once(bot, engine=object())

    prop.assert_called_once_with("100", "reply concisely", "observer", "1", "999")
    message.add_reaction.assert_any_await("👍")
    message.add_reaction.assert_any_await("👎")
    message.edit.assert_not_awaited()
    mark_posted.assert_called_once()
    mark_failed.assert_not_called()
    assert posted == 1


@pytest.mark.asyncio
async def test_drain_directive_proposal_guard_rejected_edits():
    """A guard-rejected proposal edits the posted message to the rejection copy
    and adds no reactions; the row still counts as posted."""
    row = _directive_proposal_row()
    message = _posted_message()
    channel = MagicMock()
    channel.send = AsyncMock(return_value=message)
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)

    with (
        patch.object(outbox, "_claim_pending", return_value=[row]),
        patch.object(outbox, "_mark_posted") as mark_posted,
        patch.object(outbox, "_mark_failed"),
        patch.object(directives, "propose_update", return_value=(False, "blocked")),
    ):
        await drain_once(bot, engine=object())

    message.edit.assert_awaited_once_with(content=outbox._DIRECTIVE_REJECTED)
    message.add_reaction.assert_not_awaited()
    mark_posted.assert_called_once()


@pytest.mark.asyncio
async def test_directive_proposal_hook_failure_does_not_wedge_drain():
    """A raising hook (bad payload, Discord hiccup) is swallowed: the row is
    still marked posted and the drain reports it, never stalling on it."""
    row = _directive_proposal_row()
    message = _posted_message()
    channel = MagicMock()
    channel.send = AsyncMock(return_value=message)
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)

    with (
        patch.object(outbox, "_claim_pending", return_value=[row]),
        patch.object(outbox, "_mark_posted") as mark_posted,
        patch.object(outbox, "_mark_failed") as mark_failed,
        patch.object(directives, "propose_update", side_effect=RuntimeError("boom")),
    ):
        posted = await drain_once(bot, engine=object())

    mark_posted.assert_called_once()
    mark_failed.assert_not_called()
    assert posted == 1


@pytest.mark.asyncio
async def test_drain_marks_failed_on_post_error():
    row = {
        "id": 3,
        "channel_id": "bad",
        "content": "x",
        "embed_json": None,
        "level": "info",
    }
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=None)
    bot.fetch_channel = AsyncMock(side_effect=RuntimeError("no such channel"))

    with (
        patch.object(outbox, "_claim_pending", return_value=[row]),
        patch.object(outbox, "_mark_posted") as mark_posted,
        patch.object(outbox, "_mark_failed") as mark_failed,
    ):
        posted = await drain_once(bot, engine=object())

    mark_failed.assert_called_once()
    mark_posted.assert_not_called()
    assert posted == 0
