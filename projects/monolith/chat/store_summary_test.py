"""Tests for MessageStore summary and username lookup methods."""

from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from chat.models import Message, UserChannelSummary
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


class TestFindUserIdByUsername:
    @pytest.mark.asyncio
    async def test_finds_user_by_username_in_channel(self, store, session):
        """find_user_id_by_username returns the user_id for a known username."""
        await store.save_message("1", "ch1", "u42", "Alice", "hello", False)
        result = store.find_user_id_by_username("ch1", "Alice")
        assert result == "u42"

    @pytest.mark.asyncio
    async def test_returns_none_for_unknown_username(self, store):
        """find_user_id_by_username returns None when username not found."""
        result = store.find_user_id_by_username("ch1", "Nobody")
        assert result is None

    @pytest.mark.asyncio
    async def test_scoped_to_channel(self, store, session):
        """find_user_id_by_username only looks in the specified channel."""
        await store.save_message("1", "ch1", "u1", "Alice", "hello", False)
        result = store.find_user_id_by_username("ch2", "Alice")
        assert result is None


class TestGetUserSummary:
    def test_returns_summary_when_exists(self, store, session):
        """get_user_summary returns the summary for a known user."""
        summary = UserChannelSummary(
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            summary="Alice talked about deployments.",
            last_message_id=1,
        )
        session.add(summary)
        session.commit()
        result = store.get_user_summary("ch1", "Alice")
        assert result is not None
        assert "deployments" in result.summary

    def test_returns_none_when_not_exists(self, store):
        """get_user_summary returns None for unknown user."""
        result = store.get_user_summary("ch1", "Nobody")
        assert result is None


class TestGetUserSummaryByUserId:
    def test_returns_summary_for_known_user_id(self, store, session):
        """get_user_summary_by_user_id resolves by the stable user_id."""
        session.add(
            UserChannelSummary(
                channel_id="ch1",
                user_id="98146497454960640",
                username="𝔲𝔤𝔩𝔶𝔟𝔬𝔶",
                summary="uglyboy critiques tech ecosystems.",
                last_message_id=1,
            )
        )
        session.commit()
        result = store.get_user_summary_by_user_id("ch1", "98146497454960640")
        assert result is not None
        assert result.username == "𝔲𝔤𝔩𝔶𝔟𝔬𝔶"
        assert "critiques" in result.summary

    def test_returns_none_for_unknown_user_id(self, store):
        """get_user_summary_by_user_id returns None when the id is absent."""
        assert store.get_user_summary_by_user_id("ch1", "404") is None

    def test_scoped_to_channel(self, store, session):
        """get_user_summary_by_user_id only matches within the given channel."""
        session.add(
            UserChannelSummary(
                channel_id="ch1",
                user_id="u1",
                username="Alice",
                summary="Alice summary.",
                last_message_id=1,
            )
        )
        session.commit()
        assert store.get_user_summary_by_user_id("ch2", "u1") is None


class TestListUserSummaries:
    def test_returns_all_summaries_for_channel(self, store, session):
        """list_user_summaries returns all summaries in the channel."""
        store.upsert_summary("ch1", "u1", "Alice", "Alice summary.", 10)
        store.upsert_summary("ch1", "u2", "Bob", "Bob summary.", 20)
        result = store.list_user_summaries("ch1")
        assert len(result) == 2
        usernames = {s.username for s in result}
        assert usernames == {"Alice", "Bob"}

    def test_returns_empty_list_when_no_summaries(self, store):
        """list_user_summaries returns [] for a channel with no summaries."""
        result = store.list_user_summaries("ch1")
        assert result == []

    def test_scoped_to_channel(self, store, session):
        """list_user_summaries only returns summaries for the given channel."""
        store.upsert_summary("ch1", "u1", "Alice", "Alice summary.", 10)
        store.upsert_summary("ch2", "u2", "Bob", "Bob summary.", 20)
        result = store.list_user_summaries("ch1")
        assert len(result) == 1
        assert result[0].username == "Alice"

    def test_ordered_by_most_recently_updated(self, store, session):
        """list_user_summaries returns most recently updated first."""
        store.upsert_summary("ch1", "u1", "Alice", "First.", 10)
        store.upsert_summary("ch1", "u2", "Bob", "Second.", 20)
        result = store.list_user_summaries("ch1")
        assert result[0].username == "Bob"
        assert result[1].username == "Alice"


class TestUpsertSummary:
    def test_inserts_new_summary(self, store, session):
        """upsert_summary creates a new summary when none exists."""
        store.upsert_summary("ch1", "u1", "Alice", "First summary.", 10)
        result = store.get_user_summary("ch1", "Alice")
        assert result is not None
        assert result.summary == "First summary."
        assert result.last_message_id == 10

    def test_updates_existing_summary(self, store, session):
        """upsert_summary updates an existing summary."""
        store.upsert_summary("ch1", "u1", "Alice", "First.", 10)
        store.upsert_summary("ch1", "u1", "Alice", "Updated.", 20)
        result = store.get_user_summary("ch1", "Alice")
        assert result.summary == "Updated."
        assert result.last_message_id == 20
