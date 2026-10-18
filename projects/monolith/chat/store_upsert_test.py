"""Tests for newest-first username resolution."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from chat.models import Message
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


class TestFindUserIdByUsernameOrdering:
    def test_returns_user_id_from_most_recent_message(self, store, session):
        """find_user_id_by_username returns the user_id from the most recent matching message.

        This matters when the same username was used by different user_ids over time,
        or when the same user created multiple messages.
        """
        t_early = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        t_late = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

        old_msg = Message(
            discord_message_id="1",
            channel_id="ch1",
            user_id="old_user_id",
            username="Alice",
            content="early message",
            is_bot=False,
            embedding=[0.0] * 1024,
            created_at=t_early,
        )
        new_msg = Message(
            discord_message_id="2",
            channel_id="ch1",
            user_id="new_user_id",
            username="Alice",
            content="late message",
            is_bot=False,
            embedding=[0.0] * 1024,
            created_at=t_late,
        )
        session.add(old_msg)
        session.add(new_msg)
        session.commit()

        result = store.find_user_id_by_username("ch1", "Alice")
        assert result == "new_user_id"

    def test_returns_correct_user_when_many_messages(self, store, session):
        """find_user_id_by_username works correctly when the user has many messages."""
        base_time = datetime(2025, 1, 1, tzinfo=timezone.utc)
        for i in range(10):
            session.add(
                Message(
                    discord_message_id=str(i),
                    channel_id="ch1",
                    user_id="u1",
                    username="Alice",
                    content=f"message {i}",
                    is_bot=False,
                    embedding=[0.0] * 1024,
                    created_at=datetime(2025, 1, i + 1, tzinfo=timezone.utc),
                )
            )
        session.commit()

        result = store.find_user_id_by_username("ch1", "Alice")
        assert result == "u1"
