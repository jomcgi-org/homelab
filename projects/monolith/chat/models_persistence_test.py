"""Tests for UserChannelSummary SQLModel -- persistence to DB, uniqueness, and retrieval."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from chat.models import UserChannelSummary


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


class TestUserChannelSummaryPersistence:
    def test_saves_and_retrieves_all_fields(self, session):
        """UserChannelSummary persisted to DB can be retrieved with all fields intact."""
        summary = UserChannelSummary(
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            summary="Alice talked about deployments.",
            last_message_id=42,
        )
        session.add(summary)
        session.commit()
        session.refresh(summary)

        retrieved = session.get(UserChannelSummary, summary.id)
        assert retrieved is not None
        assert retrieved.channel_id == "ch1"
        assert retrieved.user_id == "u1"
        assert retrieved.username == "Alice"
        assert retrieved.summary == "Alice talked about deployments."
        assert retrieved.last_message_id == 42

    def test_id_is_none_before_commit_and_set_after(self, session):
        """id is None before persisting and auto-assigned after commit."""
        summary = UserChannelSummary(
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            summary="test",
            last_message_id=1,
        )
        assert summary.id is None
        session.add(summary)
        session.commit()
        session.refresh(summary)
        assert summary.id is not None

    def test_multiple_users_same_channel_all_persist(self, session):
        """Multiple distinct users in the same channel can each have a summary row."""
        for user_id, username in [("u1", "Alice"), ("u2", "Bob"), ("u3", "Carol")]:
            session.add(
                UserChannelSummary(
                    channel_id="ch1",
                    user_id=user_id,
                    username=username,
                    summary=f"{username}'s summary",
                    last_message_id=1,
                )
            )
        session.commit()

        results = session.exec(
            select(UserChannelSummary).where(UserChannelSummary.channel_id == "ch1")
        ).all()
        assert len(results) == 3
        usernames = {r.username for r in results}
        assert usernames == {"Alice", "Bob", "Carol"}

    def test_same_user_multiple_channels_allowed(self, session):
        """The same user_id in different channels does not violate the unique constraint."""
        for channel_id in ["ch1", "ch2", "ch3"]:
            session.add(
                UserChannelSummary(
                    channel_id=channel_id,
                    user_id="u1",
                    username="Alice",
                    summary=f"Alice in {channel_id}",
                    last_message_id=1,
                )
            )
        session.commit()

        results = session.exec(
            select(UserChannelSummary).where(UserChannelSummary.user_id == "u1")
        ).all()
        assert len(results) == 3

    def test_unique_constraint_on_channel_user(self, session):
        """Inserting two rows with the same (channel_id, user_id) raises IntegrityError."""
        session.add(
            UserChannelSummary(
                channel_id="ch1",
                user_id="u1",
                username="Alice",
                summary="first",
                last_message_id=1,
            )
        )
        session.commit()

        session.add(
            UserChannelSummary(
                channel_id="ch1",
                user_id="u1",
                username="Alice",
                summary="duplicate",
                last_message_id=2,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
