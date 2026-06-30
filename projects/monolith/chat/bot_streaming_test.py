"""Tests for the streaming response flow in the Discord bot."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic_ai import (
    AgentRunResultEvent,
    FunctionToolCallEvent,
    PartDeltaEvent,
    TextPartDelta,
    ThinkingPartDelta,
)
from pydantic_ai.messages import ToolCallPart

from chat.bot import ChatBot, BotMessageView, STREAM_EDIT_INTERVAL


# ---------------------------------------------------------------------------
# Shared helpers (same pattern as bot_thinking_test.py)
# ---------------------------------------------------------------------------


class _AsyncCtxManager:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _async_cm():
    return _AsyncCtxManager()


def _make_bot() -> ChatBot:
    with (
        patch("chat.bot.EmbeddingClient") as mock_ec,
        patch("chat.bot.create_agent") as mock_ca,
    ):
        mock_ec.return_value = AsyncMock()
        mock_ca.return_value = MagicMock()
        bot = ChatBot()
    bot._connection = MagicMock()
    bot._connection.user = MagicMock()
    bot._connection.user.id = 999
    bot._connection.user.display_name = "BotUser"
    return bot


def _make_message(content="hello", mentions=None, msg_id=1):
    msg = MagicMock()
    msg.id = msg_id
    msg.content = content
    msg.author.bot = False
    msg.author.id = 42
    msg.author.display_name = "TestUser"
    msg.channel.id = 99
    msg.channel.typing = MagicMock(return_value=_async_cm())
    msg.mentions = mentions if mentions is not None else []
    msg.reference = None
    msg.attachments = []
    msg.embeds = []
    sent = MagicMock(id=100)
    sent.edit = AsyncMock()
    msg.reply = AsyncMock(return_value=sent)
    return msg


def _make_store():
    """Create a mock MessageStore with standard defaults."""
    mock_store = AsyncMock()
    mock_store.save_message = AsyncMock()
    mock_store.get_recent = MagicMock(return_value=[])
    mock_store.get_attachments = MagicMock(return_value={})
    mock_store.get_channel_summary = MagicMock(return_value=None)
    mock_store.get_user_summaries_for_users = MagicMock(return_value=[])
    mock_store.acquire_lock = MagicMock(return_value=True)
    mock_store.mark_completed = MagicMock()
    return mock_store


# ---------------------------------------------------------------------------
# Fake event constructors using real PydanticAI dataclass types
# ---------------------------------------------------------------------------


def _text_delta(content: str) -> PartDeltaEvent:
    """Create a PartDeltaEvent with a TextPartDelta."""
    return PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=content))


def _thinking_delta(content: str) -> PartDeltaEvent:
    """Create a PartDeltaEvent with a ThinkingPartDelta."""
    return PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta=content))


def _tool_call_event(tool_name: str, args: dict) -> FunctionToolCallEvent:
    """Create a FunctionToolCallEvent with dict args."""
    part = ToolCallPart(tool_name=tool_name, args=args)
    return FunctionToolCallEvent(part=part)


def _tool_call_event_str_args(tool_name: str, args_str: str) -> FunctionToolCallEvent:
    """Create a FunctionToolCallEvent with string (JSON-encoded) args."""
    part = ToolCallPart(tool_name=tool_name, args=args_str)
    return FunctionToolCallEvent(part=part)


def _agent_run_result_event(output: str) -> AgentRunResultEvent:
    """Create an AgentRunResultEvent with a mock result holding the given output."""
    mock_result = MagicMock()
    mock_result.output = output
    return AgentRunResultEvent(result=mock_result)


async def _async_iter(events):
    """Convert a list into an async iterator."""
    for e in events:
        yield e


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTextOnlyResponse:
    @pytest.mark.asyncio
    async def test_text_only_response_sends_and_edits(self):
        """Text chunks arrive, initial reply is sent, final edit has full text."""
        bot = _make_bot()
        bot_user = bot.user
        message = _make_message(content="Hi", mentions=[bot_user])
        mock_store = _make_store()

        events = [
            _text_delta("Hello "),
            _text_delta("world!"),
        ]

        bot.agent.run_stream_events = MagicMock(return_value=_async_iter(events))

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

        # Initial reply was sent with first text chunk
        message.reply.assert_called()
        first_reply_text = message.reply.call_args_list[0][0][0]
        assert "Hello " in first_reply_text

        # Final edit contains full response
        sent = message.reply.return_value
        last_edit_call = sent.edit.call_args_list[-1]
        assert last_edit_call.kwargs.get("content") == "Hello world!"


class TestToolCallIndicator:
    @pytest.mark.asyncio
    async def test_tool_call_shows_searching_indicator(self):
        """Tool call event shows 'Searching...' with bullet for the query."""
        bot = _make_bot()
        bot_user = bot.user
        message = _make_message(content="Hi", mentions=[bot_user])
        mock_store = _make_store()

        events = [
            _tool_call_event("web_search", {"query": "latest news"}),
            _text_delta("Here are the results."),
        ]

        bot.agent.run_stream_events = MagicMock(return_value=_async_iter(events))

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

        # The first reply should contain the searching indicator
        first_content = message.reply.call_args_list[0][0][0]
        assert "Searching" in first_content
        assert "latest news" in first_content


class TestThinkingCollected:
    @pytest.mark.asyncio
    async def test_thinking_collected_for_button(self):
        """Thinking events are collected and ThinkingView is attached on final edit."""
        bot = _make_bot()
        bot_user = bot.user
        message = _make_message(content="Hi", mentions=[bot_user])
        mock_store = _make_store()

        events = [
            _thinking_delta("Let me think"),
            _thinking_delta(" about this."),
            _text_delta("Here is my answer."),
        ]

        bot.agent.run_stream_events = MagicMock(return_value=_async_iter(events))

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

        # Final edit should have ThinkingView
        sent = message.reply.return_value
        final_edit = sent.edit.call_args_list[-1]
        view = final_edit.kwargs.get("view")
        assert isinstance(view, BotMessageView)
        assert view.thinking_text == "Let me think about this."


class TestMultipleToolCalls:
    @pytest.mark.asyncio
    async def test_multiple_tool_calls_accumulate(self):
        """Multiple tool calls show multiple bullets."""
        bot = _make_bot()
        bot_user = bot.user
        message = _make_message(content="Hi", mentions=[bot_user])
        mock_store = _make_store()

        events = [
            _tool_call_event("web_search", {"query": "first query"}),
            _tool_call_event("search_history", {"query": "second query"}),
            _text_delta("Combined results."),
        ]

        bot.agent.run_stream_events = MagicMock(return_value=_async_iter(events))

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

        # Check that both queries appear in edit calls
        sent = message.reply.return_value
        all_edit_contents = [
            call.kwargs.get("content", "") for call in sent.edit.call_args_list
        ]
        # At least one edit should contain both bullets
        combined = " ".join(all_edit_contents)
        assert "first query" in combined
        assert "second query" in combined


class TestNoEventsFallback:
    @pytest.mark.asyncio
    async def test_no_events_sends_fallback(self):
        """Empty event stream sends fallback message."""
        bot = _make_bot()
        bot_user = bot.user
        message = _make_message(content="Hi", mentions=[bot_user])
        mock_store = _make_store()

        # Empty stream
        bot.agent.run_stream_events = MagicMock(return_value=_async_iter([]))

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

        # Should have sent a fallback message
        reply_text = message.reply.call_args_list[0][0][0]
        assert "Sorry" in reply_text
        assert "trouble" in reply_text


# ---------------------------------------------------------------------------
# New gap-coverage tests
# ---------------------------------------------------------------------------


class TestAgentRunResultEvent:
    @pytest.mark.asyncio
    async def test_result_output_overrides_accumulated_text(self):
        """AgentRunResultEvent.result.output is used as the authoritative response_text,
        overriding any content accumulated from TextPartDelta events."""
        bot = _make_bot()
        bot_user = bot.user
        message = _make_message(content="Hi", mentions=[bot_user])
        mock_store = _make_store()

        events = [
            _text_delta("Partial "),
            _text_delta("text."),
            _agent_run_result_event("Authoritative final answer."),
        ]
        bot.agent.run_stream_events = MagicMock(return_value=_async_iter(events))

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

        # The bot-response save_message call must use the authoritative output
        bot_saves = [
            c
            for c in mock_store.save_message.call_args_list
            if c.kwargs.get("is_bot") is True
        ]
        assert bot_saves, "Expected bot response to be saved"
        assert bot_saves[0].kwargs["content"] == "Authoritative final answer."

    @pytest.mark.asyncio
    async def test_result_output_alone_without_text_deltas(self):
        """AgentRunResultEvent is the only text source — no TextPartDelta events."""
        bot = _make_bot()
        bot_user = bot.user
        message = _make_message(content="Hi", mentions=[bot_user])
        mock_store = _make_store()

        events = [
            _agent_run_result_event("Only from result."),
        ]
        bot.agent.run_stream_events = MagicMock(return_value=_async_iter(events))

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

        bot_saves = [
            c
            for c in mock_store.save_message.call_args_list
            if c.kwargs.get("is_bot") is True
        ]
        assert bot_saves, "Expected bot response to be saved"
        assert bot_saves[0].kwargs["content"] == "Only from result."

    @pytest.mark.asyncio
    async def test_empty_result_output_does_not_override(self):
        """An AgentRunResultEvent with empty/None output leaves response_text unchanged."""
        bot = _make_bot()
        bot_user = bot.user
        message = _make_message(content="Hi", mentions=[bot_user])
        mock_store = _make_store()

        mock_result = MagicMock()
        mock_result.output = ""  # empty string — should be falsy, ignored
        empty_result_event = AgentRunResultEvent(result=mock_result)

        events = [
            _text_delta("From delta. "),
            empty_result_event,
        ]
        bot.agent.run_stream_events = MagicMock(return_value=_async_iter(events))

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

        bot_saves = [
            c
            for c in mock_store.save_message.call_args_list
            if c.kwargs.get("is_bot") is True
        ]
        assert bot_saves, "Expected bot response to be saved"
        # Empty output is falsy — delta text must survive
        assert bot_saves[0].kwargs["content"] == "From delta. "


class TestJsonStringArgs:
    @pytest.mark.asyncio
    async def test_json_string_args_parsed_and_query_displayed(self):
        """When tool call args arrive as a JSON string, the query is extracted and shown."""
        bot = _make_bot()
        bot_user = bot.user
        message = _make_message(content="Hi", mentions=[bot_user])
        mock_store = _make_store()

        events = [
            _tool_call_event_str_args("web_search", '{"query": "Python asyncio"}'),
            _text_delta("Here are results."),
        ]
        bot.agent.run_stream_events = MagicMock(return_value=_async_iter(events))

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

        # The searching indicator should show the decoded query, not raw JSON
        first_content = message.reply.call_args_list[0][0][0]
        assert "Searching" in first_content
        assert "Python asyncio" in first_content
        assert '{"query"' not in first_content  # raw JSON must NOT appear

    @pytest.mark.asyncio
    async def test_malformed_json_string_args_falls_back_to_str(self):
        """Malformed JSON args string falls back to str(args) for the bullet."""
        bot = _make_bot()
        bot_user = bot.user
        message = _make_message(content="Hi", mentions=[bot_user])
        mock_store = _make_store()

        malformed = "not valid json {"
        events = [
            _tool_call_event_str_args("web_search", malformed),
            _text_delta("Result."),
        ]
        bot.agent.run_stream_events = MagicMock(return_value=_async_iter(events))

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

        # The raw string itself becomes the bullet text
        first_content = message.reply.call_args_list[0][0][0]
        assert "Searching" in first_content
        assert malformed in first_content

    @pytest.mark.asyncio
    async def test_json_string_without_query_key_uses_str_of_args(self):
        """JSON string args without a 'query' key fall back to str(args) for bullet."""
        bot = _make_bot()
        bot_user = bot.user
        message = _make_message(content="Hi", mentions=[bot_user])
        mock_store = _make_store()

        events = [
            _tool_call_event_str_args("lookup", '{"id": 42}'),
            _text_delta("Found it."),
        ]
        bot.agent.run_stream_events = MagicMock(return_value=_async_iter(events))

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

        # No 'query' key → str({'id': 42}) used as bullet
        first_content = message.reply.call_args_list[0][0][0]
        assert "Searching" in first_content
        assert "id" in first_content


class TestNonDictArgsFallback:
    @pytest.mark.asyncio
    async def test_list_args_falls_back_to_str(self):
        """Non-dict parsed args (list) fall back to str(args) for the bullet."""
        bot = _make_bot()
        bot_user = bot.user
        message = _make_message(content="Hi", mentions=[bot_user])
        mock_store = _make_store()

        # Pass a JSON list as a string — parses to a list, not a dict
        events = [
            _tool_call_event_str_args("search", '["term1", "term2"]'),
            _text_delta("Results."),
        ]
        bot.agent.run_stream_events = MagicMock(return_value=_async_iter(events))

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

        # str(["term1", "term2"]) contains "term1"
        first_content = message.reply.call_args_list[0][0][0]
        assert "Searching" in first_content
        assert "term1" in first_content

    @pytest.mark.asyncio
    async def test_plain_string_after_failed_json_parse_shown_as_str(self):
        """A plain (non-JSON) string arg, which stays a string after failed parse,
        is passed through str() for the bullet."""
        bot = _make_bot()
        bot_user = bot.user
        message = _make_message(content="Hi", mentions=[bot_user])
        mock_store = _make_store()

        events = [
            _tool_call_event_str_args("lookup", "plain string arg"),
            _text_delta("Result."),
        ]
        bot.agent.run_stream_events = MagicMock(return_value=_async_iter(events))

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
        ):
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

        first_content = message.reply.call_args_list[0][0][0]
        assert "Searching" in first_content
        assert "plain string arg" in first_content


class TestEditIfDueRateLimiting:
    @pytest.mark.asyncio
    async def test_rapid_text_deltas_do_not_trigger_edit_on_every_delta(self):
        """When text deltas arrive faster than STREAM_EDIT_INTERVAL, sent.edit is
        NOT called for each delta — only the first (after last_edit_time=0) and
        the final force=True edit should fire."""
        bot = _make_bot()
        message = _make_message(content="Hi", mentions=[bot.user])
        mock_store = _make_store()

        # Simulate time: first call is large (passes the 0→now threshold),
        # subsequent calls are only milliseconds apart (within STREAM_EDIT_INTERVAL).
        time_values = [
            1000.0,  # delta 1: _edit_if_due — 1000.0 - 0.0 >= 1.0 → edits
            1000.05,  # delta 2: _edit_if_due — 0.05 < 1.0 → skips
            1000.09,  # delta 3: _edit_if_due — 0.09 < 1.0 → skips
            1000.10,  # delta 4: _edit_if_due — 0.10 < 1.0 → skips
            1000.11,  # final force=True edit
        ]

        events = [
            _text_delta("chunk1"),
            _text_delta("chunk2"),
            _text_delta("chunk3"),
            _text_delta("chunk4"),
        ]
        bot.agent.run_stream_events = MagicMock(return_value=_async_iter(events))

        mock_loop = MagicMock()
        mock_loop.time.side_effect = time_values

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
            patch("chat.bot.asyncio") as mock_asyncio,
        ):
            mock_asyncio.get_event_loop.return_value = mock_loop
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

        sent = message.reply.return_value
        # With 4 deltas, edit is called at most twice (delta 1 + force=True final),
        # NOT 4 times (one per delta).
        assert sent.edit.call_count < len(events)

    @pytest.mark.asyncio
    async def test_force_true_always_triggers_edit_regardless_of_interval(self):
        """When force=True is passed (e.g. for tool calls), sent.edit fires even
        when within STREAM_EDIT_INTERVAL of the last edit."""
        bot = _make_bot()
        message = _make_message(content="Hi", mentions=[bot.user])
        mock_store = _make_store()

        # Two tool calls very close together — both should trigger forced edits
        time_values = [
            1000.0,  # tool call 1: _edit_if_due(force=True)
            1000.01,  # tool call 2: _edit_if_due(force=True) — within interval
            1000.02,  # text delta: _edit_if_due — within interval → skips
            1000.03,  # final force=True edit
        ]

        events = [
            _tool_call_event("web_search", {"query": "first"}),
            _tool_call_event("web_search", {"query": "second"}),
            _text_delta("Answer."),
        ]
        bot.agent.run_stream_events = MagicMock(return_value=_async_iter(events))

        mock_loop = MagicMock()
        mock_loop.time.side_effect = time_values

        with (
            patch("chat.bot.get_engine"),
            patch("chat.bot.Session") as mock_session_cls,
            patch("chat.bot.MessageStore", return_value=mock_store),
            patch("chat.bot.asyncio") as mock_asyncio,
        ):
            mock_asyncio.get_event_loop.return_value = mock_loop
            ctx = MagicMock()
            mock_session_cls.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_session_cls.return_value.__exit__ = MagicMock(return_value=False)
            await bot.on_message(message)

        sent = message.reply.return_value
        all_edit_contents = [
            c.kwargs.get("content", "") for c in sent.edit.call_args_list
        ]
        combined = " ".join(all_edit_contents)
        # Both tool queries must appear in edit calls despite rapid arrival
        assert "first" in combined
        assert "second" in combined
