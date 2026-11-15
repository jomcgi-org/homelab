"""Tests for generate_summaries() -- Message.id None / null handling.

Message.id is typed int | None.  If a stored message somehow has a None id,
the expression `max(m.id for m in new_messages)` raises a TypeError when Python
tries to compare None with an int.  The per-pair try/except in generate_summaries
is expected to catch this, log it, and continue processing other (channel, user)
pairs — so the function must not propagate the exception.

Also tests the related edge case where new_messages fetched from the DB is empty
(the `if not new_messages: continue` guard), ensuring no LLM call is made.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from chat.models import Message, UserChannelSummary
from chat.summarizer import generate_summaries


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


def _add_message(session, channel_id, user_id, username, content, msg_id, is_bot=False):
    msg = Message(
        id=msg_id,
        discord_message_id=str(msg_id),
        channel_id=channel_id,
        user_id=user_id,
        username=username,
        content=content,
        is_bot=is_bot,
        embedding=[0.0] * 1024,
        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
    )
    session.add(msg)
    session.commit()
    session.refresh(msg)
    return msg


class TestGenerateSummariesNoneIdHandling:
    @pytest.mark.asyncio
    async def test_none_id_exception_caught_does_not_propagate(self):
        """When Message.id is None, the TypeError in max() is caught per-pair.

        generate_summaries wraps each (channel, user) pair in try/except Exception.
        A TypeError from `max(m.id for m in new_messages)` is therefore caught and
        logged rather than propagating.  No summary should be created for the
        affected pair.
        """
        # We can't actually insert a message with id=None into SQLite (auto-assign),
        # so we simulate the scenario using a mock session that returns messages
        # with None ids from the exec() call.

        mock_session = MagicMock()

        # Two messages both with id=None: max() must compare them, raising TypeError
        # ("'<' not supported between instances of 'NoneType' and 'NoneType'").
        # A single-element iterable would return None without comparison.
        def _none_msg(discord_id: str, content: str) -> Message:
            return Message(
                id=None,
                discord_message_id=discord_id,
                channel_id="ch1",
                user_id="u1",
                username="Alice",
                content=content,
                is_bot=False,
                embedding=[0.0] * 1024,
                created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
            )

        # Each exec() call returns an object whose .all() or .first() is invoked:
        #   call 1: pairs  — code calls .all()
        #   call 2: existing UserChannelSummary — code calls .first()
        #   call 3: new messages (two None-id rows) — code calls .all()
        mock_session.exec.side_effect = [
            MagicMock(all=MagicMock(return_value=[("ch1", "u1", "Alice")])),
            MagicMock(first=MagicMock(return_value=None)),
            MagicMock(
                all=MagicMock(
                    return_value=[_none_msg("x1", "hello"), _none_msg("x2", "world")]
                )
            ),
        ]
        mock_llm = AsyncMock(return_value="some summary")

        # Should NOT raise — exception is caught inside the per-pair handler
        await generate_summaries(mock_session, mock_llm)

        # LLM should not have been called because max() raised TypeError before reaching it
        mock_llm.assert_not_called()

    @pytest.mark.asyncio
    async def test_processes_next_pair_after_none_id_failure(self, session):
        """Processing continues for the next (channel, user) pair after a per-pair error.

        This is a real-database variant: Alice's pair will be skipped because
        we inject an error via a mocked LLM, while Bob's pair gets a real summary.
        The key insight is that generate_summaries processes each pair independently
        — one failure must not prevent subsequent pairs from being processed.
        """
        _add_message(session, "ch1", "u1", "Alice", "Hello world", 1)
        _add_message(session, "ch2", "u2", "Bob", "Goodbye world", 2)

        call_order = []

        async def flaky_llm(prompt: str) -> str:
            if "Alice" in prompt:
                call_order.append("alice_fail")
                raise TypeError("simulated TypeError from max(None)")
            call_order.append("bob_ok")
            return "Bob discussed farewells."

        await generate_summaries(session, flaky_llm)

        # Alice's summary should not exist
        alice_summary = session.exec(
            select(UserChannelSummary).where(UserChannelSummary.user_id == "u1")
        ).first()
        assert alice_summary is None

        # Bob's summary should exist
        bob_summary = session.exec(
            select(UserChannelSummary).where(UserChannelSummary.user_id == "u2")
        ).first()
        assert bob_summary is not None
        assert bob_summary.summary == "Bob discussed farewells."

    @pytest.mark.asyncio
    async def test_no_llm_call_when_all_messages_already_summarised(self, session):
        """No LLM call when high_water_mark covers all existing messages."""
        _add_message(session, "ch1", "u1", "Alice", "Old content", 5)

        # Summary already covers message id=5
        session.add(
            UserChannelSummary(
                channel_id="ch1",
                user_id="u1",
                username="Alice",
                summary="Existing summary.",
                last_message_id=5,
            )
        )
        session.commit()

        mock_llm = AsyncMock()
        await generate_summaries(session, mock_llm)

        mock_llm.assert_not_called()
