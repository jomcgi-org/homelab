"""Tests for chat.attention: the attention gate (ADR 035 phase 3).

Mentions/replies engage without touching the classifier; ambient channels
classify via an injected fast-model caller; the classifier fails closed on
any error and tolerates stray text around its JSON reply.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import chat.attention as attention_mod
from chat.attention import ATTENTION_THRESHOLD, evaluate, needs_agent, should_send


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
        # A hard @mention is an explicit summons: the caller keeps it on the
        # live, never-suppressed reply path even in an ambient channel.
        assert result.explicit is True
        caller.assert_not_called()

    @pytest.mark.asyncio
    async def test_mention_in_ambient_channel_is_explicit(self):
        # The bug this guards: a direct @mention inside an ambient channel must
        # still be flagged explicit so the no_reply tool / send-gate cannot eat
        # it (the mention short-circuits before the classifier either way).
        message = _make_message(mentions=[_BOT_USER])
        caller = AsyncMock()
        result = await evaluate(
            message,
            "only jump in when @-mentioned",
            _BOT_USER,
            is_ambient=True,
            _caller=caller,
        )
        assert result.engage is True
        assert result.explicit is True
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
        # A soft classifier engage is NOT an explicit summons, so it stays on
        # the suppressible ambient path (no_reply tool / send-gate can veto it).
        assert result.explicit is False
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


class TestEngagePromptGroupAddressed:
    """Pre-gate refinement (improve-ambient eps 224/227/233): a question aimed
    at the friend group ('you lot around for a game?', 'how have you been?') is
    not on its own an engage signal."""

    @pytest.mark.asyncio
    async def test_prompt_covers_group_addressed_and_question_caveat(self):
        message = _make_message(content="you boys around for any games today?")
        caller = AsyncMock(return_value='{"engage": false, "confidence": 0.2}')
        await evaluate(message, "", _BOT_USER, is_ambient=True, _caller=caller)
        prompt = caller.call_args[0][0].lower()
        # group-addressed coordination / catching up is an ignore case
        assert "the other people in the channel rather than" in prompt
        assert "sorting out plans among themselves" in prompt
        assert "catching up with each other" in prompt
        # a bare question aimed at others is not sufficient to engage
        assert "phrased as a question is not on its own a reason to engage" in prompt


class TestSendGate:
    """Post-generation send-gate (improve-ambient): a disconnected classify that
    reads the drafted reply and vetoes an ambient send that would misfire.
    Fails open; skipped when disabled."""

    @pytest.mark.asyncio
    async def test_send_true_passes(self):
        caller = AsyncMock(return_value='{"send": true}')
        assert await should_send("d", "convo", "trigger", "reply", _caller=caller)
        caller.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_false_vetoes(self):
        caller = AsyncMock(return_value='{"send": false}')
        assert (
            await should_send("d", "convo", "trigger", "reply", _caller=caller) is False
        )

    @pytest.mark.asyncio
    async def test_fails_open_on_caller_error(self):
        caller = AsyncMock(side_effect=RuntimeError("model unreachable"))
        assert await should_send("d", "convo", "trigger", "reply", _caller=caller)

    @pytest.mark.asyncio
    async def test_missing_key_defaults_to_send(self):
        caller = AsyncMock(return_value="{}")
        assert await should_send("d", "convo", "trigger", "reply", _caller=caller)

    @pytest.mark.asyncio
    async def test_extracts_json_from_surrounding_prose(self):
        caller = AsyncMock(return_value='hold on {"send": false} ok')
        assert (
            await should_send("d", "convo", "trigger", "reply", _caller=caller) is False
        )

    @pytest.mark.asyncio
    async def test_disabled_returns_true_without_calling(self, monkeypatch):
        monkeypatch.setattr(attention_mod, "SEND_GATE_ENABLED", False)
        caller = AsyncMock()
        assert await should_send("d", "convo", "trigger", "reply", _caller=caller)
        caller.assert_not_called()

    @pytest.mark.asyncio
    async def test_prompt_includes_directive_conversation_and_reply(self):
        caller = AsyncMock(return_value='{"send": true}')
        await should_send(
            "hang back here",
            "Wobblington: wanna play minecraft?",
            "wanna play minecraft?",
            "Sure thing. You hosting?",
            _caller=caller,
        )
        prompt = caller.call_args[0][0]
        assert "hang back here" in prompt
        assert "wanna play minecraft?" in prompt
        assert "Sure thing. You hosting?" in prompt

    @pytest.mark.asyncio
    async def test_prompt_carries_explicit_invitation_exception(self):
        # /improve-ambient episode 243: "Bosun calc pi to 1000 decimal places
        # then plot the distribution" was a name-addressed request whose drafted
        # reply the send-gate wrongly vetoed. The prompt must tell the gate that
        # an addressed request is an explicit invitation the barge-in and
        # "invented numbers" vetoes do not apply to.
        caller = AsyncMock(return_value='{"send": true}')
        await should_send("d", "convo", "trigger", "reply", _caller=caller)
        prompt = caller.call_args[0][0]
        assert "explicit invitation" in prompt
        assert "wanted content, not 'invented numbers'" in prompt


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
        "chat": the chat agent has run_code to render and attach the image.
        The prompt must enumerate that capability and state the bright line so
        a "chart messages per user per day" ask no longer trips to the agent
        just for containing the word "chart"."""
        message = _make_message(content="chart messages per user per day")
        caller = AsyncMock(return_value='{"needs_agent": false}')
        await needs_agent(message, _caller=caller)
        prompt = caller.call_args[0][0].lower()
        # the tool that makes an inline chart possible is named for the model
        assert "run_code" in prompt
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
        # A caller-supplied directedness signal (reply-to-bot / trigger-name) is
        # also an explicit summons.
        assert result.explicit is True
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
