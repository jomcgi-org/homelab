"""Tests for the catch_up and extract_decisions tool bodies via FunctionModel.

Drives the actual tool execution paths (not just registration), mirroring
agent_tool_execution_test.py. build_llm_caller is faked by patching
chat.summarizer.build_llm_caller -- the same seam summarizer_startup_test.py
uses -- rather than adding an injectable parameter, since any parameter on a
@agent.tool function past RunContext is exposed in the tool's JSON schema to
the LLM.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from chat.agent import ChatDeps, create_agent
from chat.models import Message


def _make_deps(store: MagicMock, channel_id: str = "ch1") -> ChatDeps:
    return ChatDeps(channel_id=channel_id, store=store, embed_client=AsyncMock())


def _make_message(
    *, username: str, content: str, minutes_ago: int, base: datetime
) -> Message:
    return Message(
        discord_message_id=f"{minutes_ago}",
        channel_id="ch1",
        user_id=username.lower(),
        username=username,
        content=content,
        is_bot=False,
        embedding=[],
        created_at=base - timedelta(minutes=minutes_ago),
    )


def _tool_once_then_done(tool_name: str) -> FunctionModel:
    """Call the named tool with no args, then finish once it returns."""

    def model_func(messages, info):  # type: ignore[type-arg]
        for msg in messages:
            if hasattr(msg, "parts"):
                for part in msg.parts:
                    if isinstance(part, ToolReturnPart):
                        return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(
            parts=[ToolCallPart(tool_name=tool_name, args={}, tool_call_id="call-1")]
        )

    return FunctionModel(model_func)


async def _run_tool_capturing_return(agent, tool_name: str, deps: ChatDeps) -> str:
    captured = []

    def model_func(messages, info):  # type: ignore[type-arg]
        for msg in messages:
            if hasattr(msg, "parts"):
                for part in msg.parts:
                    if isinstance(part, ToolReturnPart):
                        captured.append(part.content)
                        return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(
            parts=[ToolCallPart(tool_name=tool_name, args={}, tool_call_id="call-1")]
        )

    await agent.run("go", model=FunctionModel(model_func), deps=deps)
    assert len(captured) == 1
    return captured[0]


@pytest.fixture
def base_time():
    return datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# catch_up -- empty window short-circuits without building a caller
# ---------------------------------------------------------------------------


class TestCatchUpEmptyWindow:
    @pytest.mark.asyncio
    async def test_returns_nothing_to_summarize_without_building_caller(self):
        store = MagicMock()
        store.fetch_window.return_value = []
        deps = _make_deps(store)
        agent = create_agent(base_url="http://fake:8080")

        with patch("chat.summarizer.build_llm_caller") as mock_build:
            result = await _run_tool_capturing_return(agent, "catch_up", deps)

        assert (
            result
            == "Nothing to summarize yet -- this channel doesn't have any messages."
        )
        mock_build.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetches_window_for_the_channel(self):
        store = MagicMock()
        store.fetch_window.return_value = []
        deps = _make_deps(store, channel_id="my-chan")
        agent = create_agent(base_url="http://fake:8080")

        with patch("chat.summarizer.build_llm_caller"):
            await agent.run(
                "catch me up",
                model=_tool_once_then_done("catch_up"),
                deps=deps,
            )

        store.fetch_window.assert_called_once_with("my-chan")


# ---------------------------------------------------------------------------
# catch_up -- non-empty window digests via the summary mode
# ---------------------------------------------------------------------------


class TestCatchUpNonEmptyWindow:
    @pytest.mark.asyncio
    async def test_returns_digest_output_with_coverage_line(self, base_time):
        messages = [
            _make_message(
                username="Alice",
                content="we should ship Friday",
                minutes_ago=5,
                base=base_time,
            ),
            _make_message(
                username="Bob",
                content="agreed, sounds good",
                minutes_ago=1,
                base=base_time,
            ),
        ]
        store = MagicMock()
        store.fetch_window.return_value = messages
        deps = _make_deps(store)
        agent = create_agent(base_url="http://fake:8080")

        fake_caller = AsyncMock(return_value="They agreed to ship on Friday.")

        with patch("chat.summarizer.build_llm_caller", return_value=fake_caller):
            result = await _run_tool_capturing_return(agent, "catch_up", deps)

        assert "(window: 2 messages, back to " in result
        assert "They agreed to ship on Friday." in result
        fake_caller.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_uses_summary_prompt_not_decisions_prompt(self, base_time):
        messages = [
            _make_message(
                username="Alice", content="hello there", minutes_ago=1, base=base_time
            ),
        ]
        store = MagicMock()
        store.fetch_window.return_value = messages
        deps = _make_deps(store)
        agent = create_agent(base_url="http://fake:8080")

        fake_caller = AsyncMock(return_value="summary text")

        with patch("chat.summarizer.build_llm_caller", return_value=fake_caller):
            await _run_tool_capturing_return(agent, "catch_up", deps)

        prompt = fake_caller.call_args.args[0]
        assert "Summarize the following Discord conversation window" in prompt
        assert "Decisions" not in prompt


# ---------------------------------------------------------------------------
# extract_decisions -- empty window short-circuits without building a caller
# ---------------------------------------------------------------------------


class TestExtractDecisionsEmptyWindow:
    @pytest.mark.asyncio
    async def test_returns_nothing_to_extract_without_building_caller(self):
        store = MagicMock()
        store.fetch_window.return_value = []
        deps = _make_deps(store)
        agent = create_agent(base_url="http://fake:8080")

        with patch("chat.summarizer.build_llm_caller") as mock_build:
            result = await _run_tool_capturing_return(agent, "extract_decisions", deps)

        assert (
            result
            == "Nothing to extract yet -- this channel doesn't have any messages."
        )
        mock_build.assert_not_called()

    @pytest.mark.asyncio
    async def test_fetches_window_for_the_channel(self):
        store = MagicMock()
        store.fetch_window.return_value = []
        deps = _make_deps(store, channel_id="my-chan")
        agent = create_agent(base_url="http://fake:8080")

        with patch("chat.summarizer.build_llm_caller"):
            await agent.run(
                "what did we decide",
                model=_tool_once_then_done("extract_decisions"),
                deps=deps,
            )

        store.fetch_window.assert_called_once_with("my-chan")


# ---------------------------------------------------------------------------
# extract_decisions -- non-empty window digests via the decisions mode
# ---------------------------------------------------------------------------


class TestExtractDecisionsNonEmptyWindow:
    @pytest.mark.asyncio
    async def test_returns_digest_output_with_coverage_line(self, base_time):
        messages = [
            _make_message(
                username="Alice",
                content="let's ship Friday",
                minutes_ago=5,
                base=base_time,
            ),
            _make_message(
                username="Bob",
                content="I'll write the release notes",
                minutes_ago=1,
                base=base_time,
            ),
        ]
        store = MagicMock()
        store.fetch_window.return_value = messages
        deps = _make_deps(store)
        agent = create_agent(base_url="http://fake:8080")

        fake_caller = AsyncMock(return_value="Decisions: ship Friday.")

        with patch("chat.summarizer.build_llm_caller", return_value=fake_caller):
            result = await _run_tool_capturing_return(agent, "extract_decisions", deps)

        assert "(window: 2 messages, back to " in result
        assert "Decisions: ship Friday." in result
        fake_caller.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_uses_decisions_prompt_not_summary_prompt(self, base_time):
        messages = [
            _make_message(
                username="Alice", content="hello there", minutes_ago=1, base=base_time
            ),
        ]
        store = MagicMock()
        store.fetch_window.return_value = messages
        deps = _make_deps(store)
        agent = create_agent(base_url="http://fake:8080")

        fake_caller = AsyncMock(return_value="decisions text")

        with patch("chat.summarizer.build_llm_caller", return_value=fake_caller):
            await _run_tool_capturing_return(agent, "extract_decisions", deps)

        prompt = fake_caller.call_args.args[0]
        assert "extract three" in prompt
        assert "Decisions, Action Items, and Open Questions" in prompt
