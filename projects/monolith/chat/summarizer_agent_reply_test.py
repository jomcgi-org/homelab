"""Tests for the /agent conversational reply (concierge) in chat.summarizer.

Covers the channel-scoped context assembly (the provenance guarantee: nothing
authored outside the channel can enter the reply's context), the prompt shaping
(no-invent instruction, URL kept out), and the async wiring around the injected
LLM caller.
"""

from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from chat.models import ChannelSummary, Message, UserChannelSummary
from chat.summarizer import (
    _agent_reply_context,
    _build_agent_reply_prompt,
    _build_chat_reply_prompt,
    conversational_agent_reply,
    conversational_chat_reply,
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


def _msg(session, channel_id, user_id, username, content, msg_id, is_bot=False):
    m = Message(
        id=msg_id,
        discord_message_id=str(msg_id),
        channel_id=channel_id,
        user_id=user_id,
        username=username,
        content=content,
        is_bot=is_bot,
        embedding=[0.0] * 1024,
    )
    session.add(m)
    session.commit()
    return m


class TestAgentReplyContext:
    def test_includes_channel_and_user_summaries_and_recent(self, session):
        _msg(session, "ch1", "u1", "Alice", "how's the deploy going?", 1)
        _msg(session, "ch1", "u2", "Bob", "green across the board", 2)
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
        _msg(session, "ch1", "u1", "Alice", "ch1 only message", 1)
        _msg(session, "ch2", "u1", "Alice", "SECRET ch2 message", 2)
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
            _msg(session, "ch1", "u1", "Alice", f"msg{i}", i)
        ctx = _agent_reply_context(session, "ch1", recent_limit=5)
        # Only the last 5, oldest-first.
        assert "msg16" in ctx and "msg20" in ctx
        assert "msg15" not in ctx
        assert ctx.index("msg16") < ctx.index("msg20")


class TestBuildAgentReplyPrompt:
    def test_carries_summary_details_and_context(self):
        prompt = _build_agent_reply_prompt(
            "Opened a PR.", "Added a guard and a test.", "About this channel: ops."
        )
        assert "Opened a PR." in prompt
        assert "Added a guard and a test." in prompt
        assert "About this channel: ops." in prompt

    def test_instructs_no_invention_and_omits_url(self):
        prompt = _build_agent_reply_prompt("Done.", "", "ctx")
        assert "Do not invent" in prompt
        # The URL is appended by the caller, so the model prompt must not carry it.
        assert "http" not in prompt

    def test_tolerates_empty_context(self):
        prompt = _build_agent_reply_prompt("Done.", "", "")
        assert "no channel context available" in prompt

    def test_delivers_as_bosun_in_first_person(self):
        # From the member's side this is one conversation with Bosun: the reply
        # is delivered as Bosun in the first person, not as a third-person recap
        # of "the agent" (the mis-framing that produced "the agent found ...").
        prompt = _build_agent_reply_prompt("Done.", "", "ctx")
        assert "Bosun" in prompt
        assert "first person" in prompt.lower()
        assert "relay what it did" not in prompt.lower()

    def test_forbids_addressing_member_by_name(self):
        prompt = _build_agent_reply_prompt("Done.", "", "ctx")
        assert "by name" in prompt.lower()


class TestConversationalAgentReply:
    async def test_calls_llm_with_built_prompt(self, monkeypatch):
        import chat.summarizer as summarizer

        monkeypatch.setattr(
            summarizer,
            "_fetch_agent_reply_context",
            lambda channel_id: "About this channel: ops.",
        )
        mock_llm = AsyncMock(return_value="  All wrapped up.  ")

        out = await conversational_agent_reply(
            "ch1", "Opened a PR.", "guard + test", llm_call=mock_llm
        )

        assert out == "  All wrapped up.  "  # caller trims; this returns raw
        (prompt,), _ = mock_llm.call_args
        assert "Opened a PR." in prompt
        assert "About this channel: ops." in prompt


class TestBuildChatReplyPrompt:
    """The ADR 036 chat-route reply prompt: answers the member directly and does
    NOT narrate an agent run (the mis-framing the goose-reply prompt would cause)."""

    def test_frames_as_answer_not_agent_recap(self):
        prompt = _build_chat_reply_prompt("what time is it?", "", "ctx")
        assert "what time is it?" in prompt
        # It must not tell the model to relay a coding-agent run.
        assert "coding" not in prompt.lower()
        assert "do not narrate" in prompt.lower() or "not claim" in prompt.lower()

    def test_includes_guidance_when_present(self):
        prompt = _build_chat_reply_prompt(
            "is stars broken?", "Context I found: /app/stars is up.", "ctx"
        )
        assert "/app/stars is up." in prompt

    def test_omits_guidance_block_when_empty(self):
        prompt = _build_chat_reply_prompt("hi", "", "ctx")
        assert "Background to help you answer" not in prompt

    def test_tolerates_empty_context(self):
        prompt = _build_chat_reply_prompt("hi", "", "")
        assert "no channel context available" in prompt

    def test_is_bosun_first_person_and_no_name_address(self):
        prompt = _build_chat_reply_prompt("hi", "", "ctx")
        assert "Bosun" in prompt
        assert "first person" in prompt.lower()
        assert "by name" in prompt.lower()


class TestConversationalChatReply:
    async def test_calls_llm_with_chat_prompt(self, monkeypatch):
        import chat.summarizer as summarizer

        monkeypatch.setattr(
            summarizer,
            "_fetch_agent_reply_context",
            lambda channel_id: "About this channel: ops.",
        )
        mock_llm = AsyncMock(return_value="It's up as far as I know.")

        out = await conversational_chat_reply(
            "ch1", "is stars broken?", "Context I found: it is up.", llm_call=mock_llm
        )

        assert out == "It's up as far as I know."
        (prompt,), _ = mock_llm.call_args
        assert "is stars broken?" in prompt
        assert "Context I found: it is up." in prompt
        assert "About this channel: ops." in prompt
