"""Tests for channel-level summary generation."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from chat.models import ChannelSummary, Message
from chat.summarizer import generate_channel_summaries


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


def _make_message(
    session,
    channel_id,
    user_id,
    username,
    content,
    msg_id,
    is_bot=False,
    *,
    created_at=None,
):
    msg = Message(
        id=msg_id,
        discord_message_id=str(msg_id),
        channel_id=channel_id,
        user_id=user_id,
        username=username,
        content=content,
        is_bot=is_bot,
        embedding=[0.0] * 1024,
    )
    if created_at is not None:
        msg.created_at = created_at
    session.add(msg)
    session.commit()
    session.refresh(msg)
    return msg


class TestGenerateChannelSummaries:
    @pytest.mark.asyncio
    async def test_creates_summary_for_new_channel(self, session):
        _make_message(session, "ch1", "u1", "Alice", "Deployed the app", 1)
        _make_message(session, "ch1", "u2", "Bob", "Looks good", 2)
        mock_llm = AsyncMock(return_value="Channel discusses app deployments.")
        await generate_channel_summaries(session, mock_llm)
        summary = session.exec(
            select(ChannelSummary).where(ChannelSummary.channel_id == "ch1")
        ).first()
        assert summary is not None
        assert summary.summary == "Channel discusses app deployments."
        assert summary.last_message_id == 2
        assert summary.message_count == 2

    @pytest.mark.asyncio
    async def test_updates_existing_channel_summary(self, session):
        _make_message(session, "ch1", "u1", "Alice", "Old message", 1)
        session.add(
            ChannelSummary(
                channel_id="ch1",
                summary="Old channel summary.",
                message_count=1,
                last_message_id=1,
            )
        )
        session.commit()
        _make_message(session, "ch1", "u2", "Bob", "New message", 2)
        mock_llm = AsyncMock(return_value="Updated channel summary.")
        await generate_channel_summaries(session, mock_llm)
        summary = session.exec(
            select(ChannelSummary).where(ChannelSummary.channel_id == "ch1")
        ).first()
        assert summary.summary == "Updated channel summary."
        assert summary.last_message_id == 2
        assert summary.message_count == 2

    @pytest.mark.asyncio
    async def test_skips_when_no_new_messages(self, session):
        _make_message(session, "ch1", "u1", "Alice", "Old", 1)
        session.add(
            ChannelSummary(
                channel_id="ch1",
                summary="Existing.",
                message_count=1,
                last_message_id=1,
            )
        )
        session.commit()
        mock_llm = AsyncMock()
        await generate_channel_summaries(session, mock_llm)
        mock_llm.assert_not_called()

    @pytest.mark.asyncio
    async def test_includes_bot_messages(self, session):
        _make_message(session, "ch1", "u1", "Alice", "Question", 1)
        _make_message(session, "ch1", "bot", "Bot", "Answer", 2, is_bot=True)
        mock_llm = AsyncMock(return_value="Channel has Q&A.")
        await generate_channel_summaries(session, mock_llm)
        summary = session.exec(
            select(ChannelSummary).where(ChannelSummary.channel_id == "ch1")
        ).first()
        assert summary is not None
        assert summary.message_count == 2

    @pytest.mark.asyncio
    async def test_new_channel_prompt_includes_message_inputs(self, session):
        _make_message(
            session,
            "ch1",
            "u1",
            "new-channel-author-amber",
            "new-channel-content-cobalt",
            1,
            created_at=datetime(2025, 2, 3, 4, 5, tzinfo=timezone.utc),
        )
        prompts = []

        async def capture_llm(prompt):
            prompts.append(prompt)
            return "Summary."

        await generate_channel_summaries(session, capture_llm)
        assert len(prompts) == 1
        assert "new-channel-content-cobalt" in prompts[0]
        assert "new-channel-author-amber" in prompts[0]
        # SQLite round-trips created_at without tzinfo; the prompt uses minutes.
        assert "2025-02-03 04:05" in prompts[0]

    @pytest.mark.asyncio
    async def test_existing_channel_prompt_includes_only_new_channel_inputs(
        self, session
    ):
        _make_message(session, "ch1", "u1", "below-author", "below-water-content", 1)
        _make_message(session, "ch1", "u1", "boundary-author", "at-water-content", 2)
        session.add(
            ChannelSummary(
                channel_id="ch1",
                summary="prior-summary-jade",
                message_count=2,
                last_message_id=2,
            )
        )
        session.commit()
        _make_message(
            session,
            "ch1",
            "u2",
            "incremental-author-violet",
            "incremental-content-silver",
            3,
            created_at=datetime(2025, 6, 7, 8, 9, tzinfo=timezone.utc),
        )
        _make_message(
            session,
            "ch2",
            "u3",
            "foreign-author-bronze",
            "foreign-content-copper",
            4,
            created_at=datetime(2025, 10, 11, 12, 13, tzinfo=timezone.utc),
        )
        prompts = []

        async def capture_llm(prompt):
            prompts.append(prompt)
            return "Summary."

        await generate_channel_summaries(session, capture_llm)

        assert len(prompts) == 2
        channel_prompts = [p for p in prompts if "prior-summary-jade" in p]
        assert len(channel_prompts) == 1
        channel_prompt = channel_prompts[0]
        assert "incremental-content-silver" in channel_prompt
        assert "incremental-author-violet" in channel_prompt
        assert "2025-06-07 08:09" in channel_prompt
        for excluded in (
            "below-water-content",
            "below-author",
            "at-water-content",
            "boundary-author",
            "foreign-content-copper",
            "foreign-author-bronze",
            "2025-10-11 12:13",
        ):
            assert excluded not in channel_prompt

        foreign_prompts = [p for p in prompts if "foreign-content-copper" in p]
        assert len(foreign_prompts) == 1
        foreign_prompt = foreign_prompts[0]
        assert foreign_prompt != channel_prompt
        assert "foreign-author-bronze" in foreign_prompt
        assert "2025-10-11 12:13" in foreign_prompt
        assert "prior-summary-jade" not in foreign_prompt
        assert "incremental-content-silver" not in foreign_prompt

    @pytest.mark.asyncio
    async def test_handles_multiple_channels(self, session):
        _make_message(session, "ch1", "u1", "Alice", "Infra talk", 1)
        _make_message(session, "ch2", "u2", "Bob", "Gaming talk", 2)
        call_count = 0

        async def counting_llm(prompt):
            nonlocal call_count
            call_count += 1
            return f"Summary {call_count}."

        await generate_channel_summaries(session, counting_llm)
        assert call_count == 2
        s1 = session.exec(
            select(ChannelSummary).where(ChannelSummary.channel_id == "ch1")
        ).first()
        s2 = session.exec(
            select(ChannelSummary).where(ChannelSummary.channel_id == "ch2")
        ).first()
        assert s1 is not None
        assert s2 is not None

    @pytest.mark.asyncio
    async def test_continues_on_error(self, session):
        _make_message(session, "ch1", "u1", "Alice", "hello", 1)
        _make_message(session, "ch2", "u2", "Bob", "world", 2)
        call_count = 0

        async def failing_then_ok(prompt):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("LLM failed")
            return "OK summary."

        await generate_channel_summaries(session, failing_then_ok)
        all_summaries = list(session.exec(select(ChannelSummary)).all())
        assert len(all_summaries) == 1
