"""Tests for rolling summary generation."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from chat.models import ChannelSummary, Message, UserChannelSummary
from chat.summarizer import (
    _agent_reply_context,
    generate_channel_summaries,
    generate_summaries,
)


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
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    for table in SQLModel.metadata.tables.values():
        if table.name in original_schemas:
            table.schema = original_schemas[table.name]


def _make_message(session, channel_id, user_id, username, content, msg_id):
    msg = Message(
        id=msg_id,
        discord_message_id=str(msg_id),
        channel_id=channel_id,
        user_id=user_id,
        username=username,
        content=content,
        is_bot=False,
        embedding=[0.0] * 1024,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc)
        + timedelta(seconds=msg_id),
    )
    session.add(msg)
    session.commit()
    session.refresh(msg)
    return msg


class TestGenerateSummaries:
    @pytest.mark.asyncio
    async def test_creates_summary_for_new_user(self, session):
        """First run creates a new summary from scratch."""
        _make_message(session, "ch1", "u1", "Alice", "I deployed the app", 1)
        _make_message(session, "ch1", "u1", "Alice", "It went smoothly", 2)

        mock_llm = AsyncMock(return_value="Alice deployed the app successfully.")

        await generate_summaries(session, mock_llm)

        summary = session.exec(
            select(UserChannelSummary).where(
                UserChannelSummary.channel_id == "ch1",
                UserChannelSummary.user_id == "u1",
            )
        ).first()
        assert summary is not None
        assert summary.summary == "Alice deployed the app successfully."
        assert summary.last_message_id == 2

        prompt = mock_llm.await_args.args[0]
        assert "Alice" in prompt
        assert "I deployed the app" in prompt
        assert "It went smoothly" in prompt

    @pytest.mark.asyncio
    async def test_updates_existing_summary(self, session):
        """Subsequent runs update the existing summary with new messages."""
        past = datetime(2020, 1, 1, tzinfo=timezone.utc)
        _make_message(session, "ch1", "u1", "Alice", "Old message", 1)
        session.add(
            UserChannelSummary(
                channel_id="ch1",
                user_id="u1",
                username="alice_old",
                summary="Alice said old things.",
                last_message_id=1,
                updated_at=past,
            )
        )
        session.commit()

        _make_message(session, "ch1", "u1", "Alice", "Brand new infrastructure idea", 2)

        mock_llm = AsyncMock(return_value="Alice said old and new things.")

        await generate_summaries(session, mock_llm)

        summary = session.exec(
            select(UserChannelSummary).where(
                UserChannelSummary.user_id == "u1",
            )
        ).first()
        assert summary.summary == "Alice said old and new things."
        assert summary.last_message_id == 2
        assert summary.user_id == "u1"
        assert summary.username == "Alice"
        assert summary.updated_at.replace(tzinfo=timezone.utc) > past
        assert (
            session.exec(
                select(UserChannelSummary).where(
                    UserChannelSummary.username == "alice_old"
                )
            ).first()
            is None
        )

        prompt = mock_llm.await_args.args[0]
        assert "Alice said old things." in prompt
        assert "Brand new infrastructure idea" in prompt

    @pytest.mark.asyncio
    async def test_skips_when_no_new_messages(self, session):
        """No LLM call when there are no new messages since last summary."""
        _make_message(session, "ch1", "u1", "Alice", "Old", 1)
        session.add(
            UserChannelSummary(
                channel_id="ch1",
                user_id="u1",
                username="Alice",
                summary="Existing.",
                last_message_id=1,
            )
        )
        session.commit()

        mock_llm = AsyncMock()

        await generate_summaries(session, mock_llm)

        mock_llm.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_bot_messages(self, session):
        """Bot messages are not included in summaries."""
        msg = Message(
            id=1,
            discord_message_id="1",
            channel_id="ch1",
            user_id="bot",
            username="Bot",
            content="I am a bot",
            is_bot=True,
            embedding=[0.0] * 1024,
        )
        session.add(msg)
        session.commit()

        mock_llm = AsyncMock()

        await generate_summaries(session, mock_llm)

        mock_llm.assert_not_called()

    @pytest.mark.asyncio
    async def test_survives_llm_failure(self, session):
        """An LLM failure is swallowed: the loop survives and no summary is written."""
        _make_message(session, "ch1", "u1", "Alice", "Message", 1)

        mock_llm = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

        # Must not propagate the exception out of the loop.
        await generate_summaries(session, mock_llm)

        summary = session.exec(
            select(UserChannelSummary).where(
                UserChannelSummary.channel_id == "ch1",
                UserChannelSummary.user_id == "u1",
            )
        ).first()
        assert summary is None

    @pytest.mark.asyncio
    async def test_survives_db_commit_failure(self, session):
        """A commit failure is swallowed rather than propagating out of the loop."""
        _make_message(session, "ch1", "u1", "Alice", "Message", 1)

        mock_llm = AsyncMock(return_value="A summary.")

        original_commit = session.commit

        def _failing_commit():
            raise Exception("DB write failed")

        session.commit = _failing_commit
        try:
            # Must not propagate the exception out of the loop.
            await generate_summaries(session, mock_llm)
        finally:
            session.commit = original_commit


class TestGenerateChannelSummaries:
    @pytest.fixture(name="session")
    def session_fixture(self):
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

    def _make_message(self, session, channel_id, user_id, username, content, msg_id):
        msg = Message(
            id=msg_id,
            discord_message_id=str(msg_id),
            channel_id=channel_id,
            user_id=user_id,
            username=username,
            content=content,
            is_bot=False,
            embedding=[0.0] * 1024,
        )
        session.add(msg)
        session.commit()
        session.refresh(msg)
        return msg

    @pytest.mark.asyncio
    async def test_survives_llm_failure(self, session):
        """An LLM failure is swallowed: the loop survives and no summary is written."""
        self._make_message(session, "ch1", "u1", "Alice", "Message", 1)

        mock_llm = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

        # Must not propagate the exception out of the loop.
        await generate_channel_summaries(session, mock_llm)

        summary = session.exec(
            select(ChannelSummary).where(ChannelSummary.channel_id == "ch1")
        ).first()
        assert summary is None

    @pytest.mark.asyncio
    async def test_survives_db_commit_failure(self, session):
        """A commit failure is swallowed rather than propagating out of the loop."""
        self._make_message(session, "ch1", "u1", "Alice", "Message", 1)

        mock_llm = AsyncMock(return_value="A channel summary.")

        original_commit = session.commit

        def _failing_commit():
            raise Exception("DB write failed")

        session.commit = _failing_commit
        try:
            # Must not propagate the exception out of the loop.
            await generate_channel_summaries(session, mock_llm)
        finally:
            session.commit = original_commit


class TestAgentReplyContext:
    def test_includes_channel_and_user_summaries_and_recent(self, session):
        _make_message(session, "ch1", "u1", "Alice", "how's the deploy going?", 1)
        _make_message(session, "ch1", "u2", "Bob", "green across the board", 2)
        session.add(
            ChannelSummary(
                channel_id="ch1",
                summary="A homelab ops channel.",
                message_count=2,
                last_message_id=2,
            )
        )
        session.add(
            UserChannelSummary(
                channel_id="ch1",
                user_id="u1",
                username="Alice",
                summary="Alice runs the deploys.",
                last_message_id=1,
            )
        )
        session.commit()

        ctx = _agent_reply_context(session, "ch1")

        assert "A homelab ops channel." in ctx
        assert "Alice: Alice runs the deploys." in ctx
        assert "how's the deploy going?" in ctx
        assert "Bob: green across the board" in ctx

    def test_is_channel_scoped_by_provenance(self, session):
        # Content authored in another channel must never leak into ch1's context,
        # even the same user's summary in ch2.
        _make_message(session, "ch1", "u1", "Alice", "ch1 only message", 1)
        _make_message(session, "ch2", "u1", "Alice", "SECRET ch2 message", 2)
        session.add(
            ChannelSummary(
                channel_id="ch2",
                summary="SECRET ch2 channel summary.",
                message_count=1,
                last_message_id=2,
            )
        )
        session.add(
            UserChannelSummary(
                channel_id="ch2",
                user_id="u1",
                username="Alice",
                summary="SECRET ch2 user summary.",
                last_message_id=2,
            )
        )
        session.commit()

        ctx = _agent_reply_context(session, "ch1")

        assert "ch1 only message" in ctx
        assert "SECRET" not in ctx

    def test_empty_when_no_channel_data(self, session):
        assert _agent_reply_context(session, "nope") == ""

    def test_recent_is_chronological_and_limited(self, session):
        for i in range(1, 21):
            _make_message(session, "ch1", "u1", "Alice", f"msg{i}", i)
        ctx = _agent_reply_context(session, "ch1", recent_limit=5)
        # Only the last 5, oldest-first.
        assert "msg16" in ctx and "msg20" in ctx
        assert "msg15" not in ctx
        assert ctx.index("msg16") < ctx.index("msg20")
