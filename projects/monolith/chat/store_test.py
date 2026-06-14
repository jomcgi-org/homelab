"""Tests for chat message store -- storage and recall."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from chat.models import Attachment, Blob, Message
from chat.store import MessageStore


@pytest.fixture(name="session")
def session_fixture():
    """In-memory SQLite session (schema-stripped for SQLite compat)."""
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
    with Session(engine) as session:
        yield session

    for table in SQLModel.metadata.tables.values():
        if table.name in original_schemas:
            table.schema = original_schemas[table.name]


@pytest.fixture
def store(session):
    embed_client = AsyncMock()
    embed_client.embed_batch.return_value = [[0.0] * 1024]
    return MessageStore(session=session, embed_client=embed_client)


class TestSaveMessage:
    @pytest.mark.asyncio
    async def test_saves_message_to_db(self, store, session):
        """save_message persists a message to the database."""
        await store.save_message(
            discord_message_id="111",
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            content="Hello!",
            is_bot=False,
        )
        msgs = session.exec(select(Message)).all()
        assert len(msgs) == 1
        assert msgs[0].content == "Hello!"
        assert msgs[0].username == "Alice"

    @pytest.mark.asyncio
    async def test_calls_embed_client(self, store):
        """save_message calls the embedding client with the message content."""
        await store.save_message(
            discord_message_id="222",
            channel_id="ch1",
            user_id="u1",
            username="Bob",
            content="What is the weather?",
            is_bot=False,
        )
        store.embed_client.embed_batch.assert_called_once_with(["What is the weather?"])


class TestGetRecentMessages:
    @pytest.mark.asyncio
    async def test_returns_recent_messages_in_order(self, store, session):
        """get_recent returns messages ordered oldest-first."""
        for i in range(5):
            await store.save_message(
                discord_message_id=str(i),
                channel_id="ch1",
                user_id="u1",
                username="Alice",
                content=f"msg {i}",
                is_bot=False,
            )
        recent = store.get_recent("ch1", limit=3)
        assert len(recent) == 3
        assert recent[0].content == "msg 2"
        assert recent[2].content == "msg 4"

    @pytest.mark.asyncio
    async def test_filters_by_channel(self, store, session):
        """get_recent only returns messages from the specified channel."""
        await store.save_message("a", "ch1", "u1", "A", "in ch1", False)
        await store.save_message("b", "ch2", "u1", "A", "in ch2", False)
        recent = store.get_recent("ch1", limit=10)
        assert len(recent) == 1
        assert recent[0].content == "in ch1"


class TestSaveMessageWithAttachments:
    @pytest.mark.asyncio
    async def test_saves_attachments_linked_to_message(self, store, session):
        """save_message persists attachments linked to the message."""
        attachments = [
            {
                "data": b"\x89PNG",
                "content_type": "image/png",
                "filename": "photo.png",
                "description": "A cat",
            }
        ]
        msg = await store.save_message(
            discord_message_id="att1",
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            content="Look at this!",
            is_bot=False,
            attachments=attachments,
        )
        assert msg is not None
        saved = session.exec(select(Attachment)).all()
        assert len(saved) == 1
        assert saved[0].message_id == msg.id
        assert saved[0].filename == "photo.png"
        blob = session.get(Blob, saved[0].blob_sha256)
        assert blob is not None
        assert blob.description == "A cat"

    @pytest.mark.asyncio
    async def test_embeds_combined_text_and_descriptions(self, store):
        """save_message embeds text content combined with image descriptions."""
        attachments = [
            {
                "data": b"\x89PNG",
                "content_type": "image/png",
                "filename": "photo.png",
                "description": "A sunset",
            },
            {
                "data": b"\xff\xd8\xff",
                "content_type": "image/jpeg",
                "filename": "sky.jpg",
                "description": "Blue sky with clouds",
            },
        ]
        await store.save_message(
            discord_message_id="att2",
            channel_id="ch1",
            user_id="u1",
            username="Bob",
            content="Beautiful day!",
            is_bot=False,
            attachments=attachments,
        )
        embed_call = store.embed_client.embed_batch.call_args[0][0][0]
        assert "Beautiful day!" in embed_call
        assert "[Image: A sunset]" in embed_call
        assert "[Image: Blue sky with clouds]" in embed_call

    @pytest.mark.asyncio
    async def test_text_only_message_unchanged(self, store):
        """save_message without attachments behaves as before."""
        await store.save_message(
            discord_message_id="noatt",
            channel_id="ch1",
            user_id="u1",
            username="Carol",
            content="Just text",
            is_bot=False,
        )
        store.embed_client.embed_batch.assert_called_once_with(["Just text"])


class TestGetAttachments:
    @pytest.mark.asyncio
    async def test_get_attachments_for_messages(self, store, session):
        """get_attachments returns attachments keyed by message id."""
        msg = await store.save_message(
            discord_message_id="ga1",
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            content="Photo",
            is_bot=False,
            attachments=[
                {
                    "data": b"\x89PNG",
                    "content_type": "image/png",
                    "filename": "a.png",
                    "description": "Cat",
                },
            ],
        )
        result = store.get_attachments([msg.id])
        assert msg.id in result
        assert len(result[msg.id]) == 1
        att, blob = result[msg.id][0]
        assert att.filename == "a.png"
        assert blob.description == "Cat"


class TestThinking:
    @pytest.mark.asyncio
    async def test_save_message_stores_thinking(self, store):
        """Thinking text is persisted alongside the message."""
        msg = await store.save_message(
            discord_message_id="t1",
            channel_id="ch1",
            user_id="u1",
            username="Bot",
            content="Hello!",
            is_bot=True,
            thinking="my reasoning here",
        )
        assert msg is not None
        assert msg.thinking == "my reasoning here"

    @pytest.mark.asyncio
    async def test_save_message_no_thinking_defaults_none(self, store):
        """Messages saved without thinking have thinking=None."""
        msg = await store.save_message(
            discord_message_id="t2",
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            content="Hi",
            is_bot=False,
        )
        assert msg.thinking is None

    @pytest.mark.asyncio
    async def test_get_messages_with_thinking(self, store):
        """get_messages_with_thinking returns only bot messages with thinking."""
        store.embed_client.embed_batch.side_effect = [
            [[0.0] * 1024],
            [[0.0] * 1024],
            [[0.0] * 1024],
        ]

        await store.save_message(
            "m1", "ch1", "u1", "Bot", "resp1", True, thinking="thought1"
        )
        await store.save_message("m2", "ch1", "u1", "Bot", "resp2", True, thinking=None)
        await store.save_message("m3", "ch1", "u2", "Alice", "human msg", False)

        results = store.get_messages_with_thinking()
        assert len(results) == 1
        assert results[0].discord_message_id == "m1"
        assert results[0].thinking == "thought1"
