"""Focused tests for per-channel notes and summary prompt configuration."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from chat import acl
from chat.agent import ChatDeps, create_agent
from chat.channel_notes import (
    SummaryConfig,
    render_template,
    update_memory,
    validate_update,
)
from chat.models import ChannelMemory, ChannelSummary, Message, UserChannelSummary
from chat.store import MessageStore
from chat.summarizer import _user_prompt, generate_channel_summaries, generate_summaries


@pytest.fixture(name="engine")
def engine_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'channel-memory.db'}")
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


def _message(channel_id: str, msg_id: int = 1) -> Message:
    return Message(
        id=msg_id,
        discord_message_id=str(msg_id),
        channel_id=channel_id,
        user_id="user-1",
        username="Alice",
        content="Ship the release after CI passes",
        is_bot=False,
        embedding=[0.0] * 1024,
    )


def test_channel_memory_persists_and_keeps_channels_isolated(engine):
    with Session(engine) as session:
        update_memory(session, "channel-a", "notes", "Uses blue deployments", "owner")
        update_memory(session, "channel-b", "notes", "Uses green deployments", "owner")

    with Session(engine) as session:
        rows = list(session.exec(select(ChannelMemory)).all())
        assert len(rows) == 2
        assert session.get(ChannelMemory, "channel-a").notes == "Uses blue deployments"
        assert session.get(ChannelMemory, "channel-b").notes == "Uses green deployments"


def test_atomic_upsert_preserves_other_fields_across_sessions(engine):
    with Session(engine) as first:
        update_memory(first, "channel-a", "notes", "Keep this", "owner-1")
    with Session(engine) as second:
        update_memory(second, "channel-a", "summary_style", "Use bullets", "owner-2")

    with Session(engine) as session:
        row = session.get(ChannelMemory, "channel-a")
        assert row.notes == "Keep this"
        assert row.summary_style == "Use bullets"
        assert row.updated_by_user_id == "owner-2"


def test_template_validation_allows_only_safe_named_placeholders():
    template = "Summarize {username} as {summary_style}:\n{messages}\nNotes: {notes}"
    rendered = render_template(
        "summary_prompt_user",
        template,
        {
            "username": "Alice",
            "summary_style": "brief",
            "messages": "hello",
            "notes": "release channel",
        },
    )
    assert "Summarize Alice as brief" in rendered

    with pytest.raises(ValueError, match="unsupported placeholder"):
        validate_update("summary_prompt_user", "{username.__class__} {messages}")
    with pytest.raises(ValueError, match=r"must include \{messages\}"):
        validate_update("summary_prompt_channel", "No message input")


def test_summary_style_and_notes_customize_the_default_prompt():
    prompt = _user_prompt(
        username="Alice",
        messages="A message",
        current_summary="",
        config=SummaryConfig(
            style="Use decision bullets", notes="Linux CI is required"
        ),
    )
    assert "Use this summary style: Use decision bullets." in prompt
    assert "2-4 sentence" not in prompt
    assert "Durable channel notes" in prompt
    assert "Linux CI is required" in prompt


@pytest.mark.asyncio
async def test_both_rolling_summarizers_use_channel_configuration(engine):
    user_prompts: list[str] = []
    channel_prompts: list[str] = []

    async def capture_user(prompt: str) -> str:
        user_prompts.append(prompt)
        return "User result"

    async def capture_channel(prompt: str) -> str:
        channel_prompts.append(prompt)
        return "Channel result"

    with Session(engine) as session:
        session.add(_message("channel-a"))
        session.add(
            ChannelMemory(
                channel_id="channel-a",
                summary_prompt_user=(
                    "USER={username} STYLE={summary_style} NOTES={notes}\n{messages}"
                ),
                summary_prompt_channel=(
                    "CHANNEL STYLE={summary_style} NOTES={notes}\n{messages}"
                ),
                summary_style="decision bullets",
                notes="Deploy only after Linux CI",
            )
        )
        session.commit()

        await generate_summaries(session, capture_user)
        await generate_channel_summaries(session, capture_channel)

        assert len(user_prompts) == 1
        assert (
            "USER=Alice STYLE=decision bullets NOTES=Deploy only after Linux CI"
            in user_prompts[0]
        )
        assert "Ship the release after CI passes" in user_prompts[0]
        assert "CHANNEL STYLE=decision bullets" in channel_prompts[0]
        assert "NOTES=Deploy only after Linux CI" in channel_prompts[0]
        assert session.exec(select(UserChannelSummary)).first().summary == "User result"
        assert session.exec(select(ChannelSummary)).first().summary == "Channel result"


@pytest.mark.asyncio
async def test_channel_notes_tool_reads_its_channel_and_owner_updates(
    engine, monkeypatch
):
    monkeypatch.setattr(acl, "is_owner", lambda user_id: user_id == "owner")
    agent = create_agent(base_url="http://fake:8080")
    tool = agent._function_toolset.tools["channel_notes"].function

    with Session(engine) as session:
        update_memory(session, "other-channel", "notes", "private elsewhere", "owner")
        store = MessageStore(session=session, embed_client=AsyncMock())
        deps = ChatDeps(
            channel_id="current-channel",
            store=store,
            embed_client=AsyncMock(),
            author_id="owner",
        )
        ctx = SimpleNamespace(deps=deps)

        result = await tool(ctx, action="update", field="notes", value="local note")
        assert result == "Updated notes for this channel."
        read_result = await tool(ctx, action="read")
        assert "local note" in read_result
        assert "private elsewhere" not in read_result

        deps.author_id = "not-owner"
        denied = await tool(
            ctx, action="update", field="summary_style", value="very detailed"
        )
        assert denied == "Only the configured owner can update channel memory."
        assert session.get(ChannelMemory, "current-channel").summary_style is None
