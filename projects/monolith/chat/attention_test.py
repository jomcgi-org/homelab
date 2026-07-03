"""Tests for chat.attention: the attention gate (ADR 035 phase 3).

Mentions/replies engage without touching the classifier; ambient channels
classify via an injected fast-model caller; the classifier fails closed on
any error and tolerates stray text around its JSON reply.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chat.attention import ATTENTION_THRESHOLD, evaluate, needs_agent


def _make_message(content: str = "hey", mentions=None, reference=None):
    return SimpleNamespace(
        content=content,
        mentions=mentions if mentions is not None else [],
        reference=reference,
        author=SimpleNamespace(bot=False, id=1),
    )


_BOT_USER = SimpleNamespace(id=999)


class TestExplicitTrigger:
    @pytest.mark.asyncio
    async def test_mention_engages_without_calling_the_classifier(self):
        message = _make_message(mentions=[_BOT_USER])
        caller = AsyncMock()
        result = await evaluate(
            message, "", _BOT_USER, is_ambient=False, _caller=caller
        )
        assert result.engage is True
        assert result.confidence == 1.0
        caller.assert_not_called()


class TestNonAmbientNonMention:
    @pytest.mark.asyncio
    async def test_ignored_without_calling_the_classifier(self):
        message = _make_message()
        caller = AsyncMock()
        result = await evaluate(
            message, "", _BOT_USER, is_ambient=False, _caller=caller
        )
        assert result.engage is False
        assert result.confidence == 0.0
        caller.assert_not_called()


class TestAmbientClassification:
    @pytest.mark.asyncio
    async def test_engages_above_threshold(self):
        message = _make_message(content="hey can you help with this")
        caller = AsyncMock(return_value='{"engage": true, "confidence": 0.9}')
        result = await evaluate(
            message, "help with code", _BOT_USER, is_ambient=True, _caller=caller
        )
        assert result.engage is True
        assert result.confidence == 0.9
        caller.assert_called_once()

    @pytest.mark.asyncio
    async def test_ignores_below_threshold(self):
        message = _make_message(content="maybe you could help")
        caller = AsyncMock(return_value='{"engage": true, "confidence": 0.3}')
        result = await evaluate(
            message, "help with code", _BOT_USER, is_ambient=True, _caller=caller
        )
        assert result.confidence < ATTENTION_THRESHOLD
        assert result.engage is False

    @pytest.mark.asyncio
    async def test_fails_closed_on_caller_error(self):
        message = _make_message(content="anything")
        caller = AsyncMock(side_effect=RuntimeError("model unreachable"))
        result = await evaluate(message, "", _BOT_USER, is_ambient=True, _caller=caller)
        assert result.engage is False
        assert result.confidence == 0.0

    @pytest.mark.asyncio
    async def test_extracts_json_from_surrounding_prose(self):
        message = _make_message(content="anything")
        caller = AsyncMock(return_value='sure! {"engage": true, "confidence": 0.95} ok')
        result = await evaluate(message, "", _BOT_USER, is_ambient=True, _caller=caller)
        assert result.engage is True
        assert result.confidence == 0.95


class TestRecentTagWeighting:
    """ADR 035 engagement policy: recently_tagged lowers the engage threshold."""

    @pytest.mark.asyncio
    async def test_recently_tagged_engages_below_default_threshold(self):
        message = _make_message(content="is that actually true though?")
        caller = AsyncMock(return_value='{"engage": true, "confidence": 0.4}')
        result = await evaluate(
            message,
            "",
            _BOT_USER,
            is_ambient=True,
            recently_tagged=True,
            _caller=caller,
        )
        assert result.confidence == 0.4
        assert result.confidence < ATTENTION_THRESHOLD
        assert result.engage is True

    @pytest.mark.asyncio
    async def test_same_confidence_ignored_without_recent_tag(self):
        message = _make_message(content="is that actually true though?")
        caller = AsyncMock(return_value='{"engage": true, "confidence": 0.4}')
        result = await evaluate(
            message,
            "",
            _BOT_USER,
            is_ambient=True,
            recently_tagged=False,
            _caller=caller,
        )
        assert result.confidence == 0.4
        assert result.engage is False

    @pytest.mark.asyncio
    async def test_mention_still_short_circuits_without_a_caller(self):
        message = _make_message(mentions=[_BOT_USER])
        caller = AsyncMock()
        result = await evaluate(
            message,
            "",
            _BOT_USER,
            is_ambient=False,
            recently_tagged=True,
            _caller=caller,
        )
        assert result.engage is True
        assert result.confidence == 1.0
        caller.assert_not_called()


class TestNeedsAgent:
    """ADR 035 Phase 4: the in-monolith depth classify (chat vs goose guest)."""

    @pytest.mark.asyncio
    async def test_repo_work_routes_to_agent(self):
        message = _make_message(content="can you fix the bug in bot.py and push a PR")
        caller = AsyncMock(return_value='{"needs_agent": true}')
        result = await needs_agent(message, _caller=caller)
        assert result is True
        caller.assert_called_once()

    @pytest.mark.asyncio
    async def test_conversation_routes_to_chat(self):
        message = _make_message(content="what's a good name for a boat?")
        caller = AsyncMock(return_value='{"needs_agent": false}')
        result = await needs_agent(message, _caller=caller)
        assert result is False

    @pytest.mark.asyncio
    async def test_fails_closed_to_chat_on_caller_error(self):
        message = _make_message(content="anything")
        caller = AsyncMock(side_effect=RuntimeError("model unreachable"))
        result = await needs_agent(message, _caller=caller)
        assert result is False

    @pytest.mark.asyncio
    async def test_extracts_json_from_surrounding_prose(self):
        message = _make_message(content="anything")
        caller = AsyncMock(return_value='sure! {"needs_agent": true} ok')
        result = await needs_agent(message, _caller=caller)
        assert result is True

    @pytest.mark.asyncio
    async def test_prompt_keeps_channel_summarization_on_chat(self):
        """The chat agent now has catch_up/extract_decisions tools (Task 1.3):
        summarizing this conversation or pulling decisions/action items out of
        channel history must stay "chat", not route to the heavy agent."""
        message = _make_message(content="can you summarize this thread?")
        caller = AsyncMock(return_value='{"needs_agent": false}')
        await needs_agent(message, _caller=caller)
        prompt = caller.call_args[0][0].lower()
        assert "summarizing this conversation" in prompt
        assert "catching up" in prompt
        assert "channel history" in prompt
        assert "decisions" in prompt
        assert "action items" in prompt

    @pytest.mark.asyncio
    async def test_prompt_still_mentions_repo_artifact_and_research(self):
        """Additive change: the existing depth-classify wording must survive."""
        message = _make_message(content="anything")
        caller = AsyncMock(return_value='{"needs_agent": false}')
        await needs_agent(message, _caller=caller)
        prompt = caller.call_args[0][0].lower()
        assert "repository/codebase" in prompt
        assert "artifact/page" in prompt
        assert "thorough multi-source" in prompt

    @pytest.mark.asyncio
    async def test_prompt_routes_python_charts_to_chat(self):
        """A chart/plot of data (including this channel's own stats) must stay
        "chat": the chat agent has run_python to render and attach the image.
        The prompt must enumerate that capability and state the bright line so
        a "chart messages per user per day" ask no longer trips to the agent
        just for containing the word "chart"."""
        message = _make_message(content="chart messages per user per day")
        caller = AsyncMock(return_value='{"needs_agent": false}')
        await needs_agent(message, _caller=caller)
        prompt = caller.call_args[0][0].lower()
        # the tool that makes an inline chart possible is named for the model
        assert "run_python" in prompt
        # the bright line: a python image is chat, a click-around page is agent
        assert "chart" in prompt
        assert "attaches" in prompt
        assert "not a single image" in prompt
        # the channel-stats capability is surfaced so the model knows the data
        # is reachable without the heavy guest
        assert "per-user or per-day breakdowns" in prompt


class TestDirectedSeam:
    """The non-Discord `directed` seam (ADR 039): a caller-supplied directedness
    signal engages without the Discord mention check, and a None bot_user (a
    non-Discord channel) never reaches should_respond."""

    @pytest.mark.asyncio
    async def test_directed_engages_without_caller_or_bot_user(self):
        message = _make_message(content="hey bosun")
        caller = AsyncMock()
        result = await evaluate(
            message, "", None, is_ambient=False, directed=True, _caller=caller
        )
        assert result.engage is True
        assert result.confidence == 1.0
        caller.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_bot_user_non_directed_non_ambient_ignores(self):
        # A non-Discord message that is neither directed nor ambient is ignored
        # without touching should_respond (which would fail on a non-Discord shape).
        message = _make_message(content="just the two of us")
        caller = AsyncMock()
        result = await evaluate(
            message, "", None, is_ambient=False, directed=False, _caller=caller
        )
        assert result.engage is False
        caller.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_bot_user_ambient_still_classifies(self):
        message = _make_message(content="anyone around?")
        caller = AsyncMock(return_value='{"engage": true, "confidence": 0.9}')
        result = await evaluate(
            message, "household", None, is_ambient=True, directed=False, _caller=caller
        )
        assert result.engage is True
        caller.assert_called_once()
