"""Tests for MessageStore.fetch_window -- bounded chronological channel window."""

from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

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


class TestFetchWindow:
    @pytest.mark.asyncio
    async def test_empty_channel_returns_empty_list(self, store):
        """A channel with no messages returns an empty list."""
        assert store.fetch_window("nochannel") == []

    @pytest.mark.asyncio
    async def test_returns_chronological_order(self, store):
        """Messages come back oldest first."""
        for i in range(5):
            await store.save_message(
                discord_message_id=str(i),
                channel_id="ch1",
                user_id="u1",
                username="Alice",
                content=f"msg {i}",
                is_bot=False,
            )
        window = store.fetch_window("ch1")
        assert [m.content for m in window] == [f"msg {i}" for i in range(5)]

    @pytest.mark.asyncio
    async def test_filters_by_channel(self, store):
        """Only messages from the requested channel are returned; others never leak."""
        await store.save_message("a", "ch1", "u1", "Alice", "in ch1", False)
        await store.save_message("b", "ch2", "u1", "Alice", "in ch2", False)
        window = store.fetch_window("ch1")
        assert len(window) == 1
        assert window[0].content == "in ch1"

    @pytest.mark.asyncio
    async def test_max_messages_caps_and_keeps_newest(self, store):
        """When max_messages is hit first, the oldest messages are dropped."""
        for i in range(10):
            await store.save_message(
                discord_message_id=str(i),
                channel_id="ch1",
                user_id="u1",
                username="Alice",
                content=f"msg {i}",
                is_bot=False,
            )
        window = store.fetch_window("ch1", max_messages=3)
        assert [m.content for m in window] == ["msg 7", "msg 8", "msg 9"]

    @pytest.mark.asyncio
    async def test_max_chars_caps_and_keeps_newest(self, store):
        """When max_chars is hit first, the oldest messages are dropped."""
        # Each message is exactly 10 chars; a cap of 25 keeps only the newest
        # 2 (20 chars), since a 3rd would push the total to 30 and cross it.
        for i in range(5):
            await store.save_message(
                discord_message_id=str(i),
                channel_id="ch1",
                user_id="u1",
                username="Alice",
                content=f"{i:010d}",
                is_bot=False,
            )
        window = store.fetch_window("ch1", max_messages=300, max_chars=25)
        assert [m.content for m in window] == ["0000000003", "0000000004"]

    @pytest.mark.asyncio
    async def test_max_chars_always_returns_newest_even_if_it_alone_exceeds_cap(
        self, store
    ):
        """The newest message is always included even if its length alone crosses
        max_chars, so a non-empty channel never returns an empty window."""
        await store.save_message(
            discord_message_id="1",
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            content="x" * 50,
            is_bot=False,
        )
        window = store.fetch_window("ch1", max_chars=10)
        assert len(window) == 1
        assert window[0].content == "x" * 50

    @pytest.mark.asyncio
    async def test_default_caps_return_full_small_channel(self, store):
        """With default caps, a small channel's whole history comes back."""
        for i in range(3):
            await store.save_message(
                discord_message_id=str(i),
                channel_id="ch1",
                user_id="u1",
                username="Alice",
                content=f"msg {i}",
                is_bot=False,
            )
        window = store.fetch_window("ch1")
        assert len(window) == 3
