"""Tests for agent helper functions and tool implementations.

Covers gaps not addressed by existing test files:
- _coerce_username: None input, dict extraction (username/name/display_name keys),
  invalid dict, non-string types
- ChatDeps: all three fields (channel_id, store, embed_client)
- web_search tool: query delegation and return-value passthrough
- search_history tool: query embedding, username coercion, similarity search,
  limit boundary (exactly 20, over 20, under 20), username=None skips user lookup
- get_user_summary tool: list path, specific-user path, dict username coercion
- build_system_prompt: exact tool descriptions, conciseness guidance, tool usage prompt
- build_system_prompt (DO/DON'T): persona ('friend hanging out'), DO:/DON'T: section
  headers and ordering, DO section phrases ('Answer directly', 'Match the vibe',
  proactive tool usage, short casual replies), DON'T section phrases
  ('contextually, they are referring to', padding small talk into an essay, filler starters
  'Sure!'/'Of course!'/'Great question!', announcing tool usage, 'as an AI')
- format_context_messages: timestamp format, bot "Assistant" label, multiple messages,
  multiple attachments per message, missing message id in attachment map
"""

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from chat.agent import (
    ChatDeps,
    _coerce_username,
    build_system_prompt,
    create_agent,
    format_context_messages,
)
from chat.models import Attachment, Blob, Message, UserChannelSummary


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_deps(
    store: MagicMock | None = None,
    embed_client: AsyncMock | None = None,
    channel_id: str = "ch1",
) -> ChatDeps:
    return ChatDeps(
        channel_id=channel_id,
        store=store or MagicMock(),
        embed_client=embed_client or AsyncMock(),
    )


def _tool_model(tool_name: str, args: dict) -> FunctionModel:
    """FunctionModel that calls one tool then returns 'done'."""

    def model_func(messages, info):  # type: ignore[type-arg]
        for msg in messages:
            if hasattr(msg, "parts"):
                for part in msg.parts:
                    if isinstance(part, ToolReturnPart):
                        return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(
            parts=[ToolCallPart(tool_name=tool_name, args=args, tool_call_id="c1")]
        )

    return FunctionModel(model_func)


def _capturing_model(tool_name: str, args: dict, captured: list) -> FunctionModel:
    """FunctionModel that captures tool return content into *captured* then returns 'done'."""

    def model_func(messages, info):  # type: ignore[type-arg]
        for msg in messages:
            if hasattr(msg, "parts"):
                for part in msg.parts:
                    if isinstance(part, ToolReturnPart):
                        captured.append(part.content)
                        return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(
            parts=[ToolCallPart(tool_name=tool_name, args=args, tool_call_id="c1")]
        )

    return FunctionModel(model_func)


def _make_summary(
    username: str = "alice",
    channel_id: str = "ch1",
    summary_text: str = "summary text",
    updated: datetime | None = None,
) -> UserChannelSummary:
    return UserChannelSummary(
        channel_id=channel_id,
        user_id="u1",
        username=username,
        summary=summary_text,
        last_message_id=1,
        updated_at=updated or datetime(2026, 4, 1, tzinfo=timezone.utc),
    )


def _make_message(
    *,
    id: int = 1,
    username: str = "Alice",
    content: str = "hello",
    is_bot: bool = False,
    created_at: datetime | None = None,
) -> Message:
    """Convenience factory for Message objects used in tests."""
    if created_at is None:
        created_at = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    return Message(
        id=id,
        discord_message_id=str(id),
        channel_id="ch1",
        user_id="u1",
        username=username,
        content=content,
        is_bot=is_bot,
        embedding=[0.0] * 1024,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# build_system_prompt — return type
# ---------------------------------------------------------------------------


class TestBuildSystemPromptReturnType:
    def test_returns_string(self):
        """build_system_prompt() returns a str instance."""
        result = build_system_prompt()
        assert isinstance(result, str)

    def test_returns_non_empty_string(self):
        """build_system_prompt() returns a non-empty string."""
        result = build_system_prompt()
        assert len(result) > 0


# ---------------------------------------------------------------------------
# build_system_prompt — content: tool guidance
# Tool-specific descriptions are now auto-generated from signposts in the
# dynamic system prompt; see TestAllToolsSignposted in agent_tools_test.py.
# ---------------------------------------------------------------------------


class TestBuildSystemPromptToolGuidance:
    def test_encourages_proactive_search(self):
        """System prompt encourages search-first behavior."""
        prompt = build_system_prompt()
        assert "Search before you respond" in prompt

    def test_includes_dont_pretend_rule(self):
        """System prompt warns against claiming to have searched without doing so."""
        prompt = build_system_prompt()
        assert "Pretend you looked something up" in prompt


# ---------------------------------------------------------------------------
# build_system_prompt — content: guidance phrases
# ---------------------------------------------------------------------------


class TestBuildSystemPromptGuidance:
    def test_includes_discord_chat_context(self):
        """System prompt identifies the agent as a Discord chat assistant."""
        prompt = build_system_prompt()
        assert "Discord" in prompt

    def test_instructs_concise_responses(self):
        """System prompt instructs the model to keep responses concise."""
        prompt = build_system_prompt()
        assert "concise" in prompt

    def test_instructs_search_first_for_factual_content(self):
        """System prompt tells the agent to search before responding to factual content."""
        prompt = build_system_prompt()
        assert "search" in prompt.lower()

    def test_instructs_matching_conversation_vibe(self):
        """System prompt tells the agent to match conversation vibe."""
        prompt = build_system_prompt()
        assert "vibe" in prompt


# ---------------------------------------------------------------------------
# format_context_messages — timestamp format
# ---------------------------------------------------------------------------


class TestFormatContextMessagesTimestamp:
    def test_timestamp_uses_year_month_day_hour_minute_format(self):
        """Timestamps appear as [YYYY-MM-DD HH:MM] in the output."""
        msg = _make_message(
            created_at=datetime(2026, 4, 1, 14, 30, tzinfo=timezone.utc),
        )
        result = format_context_messages([msg])
        assert "[2026-04-01 14:30]" in result

    def test_timestamp_is_zero_padded(self):
        """Single-digit month, day, hour, and minute values are zero-padded."""
        msg = _make_message(
            created_at=datetime(2026, 1, 5, 8, 5, tzinfo=timezone.utc),
        )
        result = format_context_messages([msg])
        assert "[2026-01-05 08:05]" in result


# ---------------------------------------------------------------------------
# format_context_messages — bot vs user label
# ---------------------------------------------------------------------------


class TestFormatContextMessagesBotLabel:
    def test_bot_message_uses_assistant_label_not_username(self):
        """Bot messages are prefixed with 'Assistant:' not the bot's username."""
        msg = _make_message(username="SomeBot", content="Beep boop", is_bot=True)
        result = format_context_messages([msg])
        assert "Assistant:" in result
        assert "SomeBot:" not in result

    def test_user_message_uses_actual_username(self):
        """User messages are prefixed with the actual username followed by a colon."""
        msg = _make_message(username="charlie", content="hey there", is_bot=False)
        result = format_context_messages([msg])
        assert "charlie:" in result
        assert "Assistant:" not in result

    def test_bot_message_content_is_included(self):
        """The content of a bot message is present in the output."""
        msg = _make_message(username="bot", content="I can help", is_bot=True)
        result = format_context_messages([msg])
        assert "I can help" in result


# ---------------------------------------------------------------------------
# format_context_messages — multiple messages
# ---------------------------------------------------------------------------


class TestFormatContextMessagesMultiple:
    def test_two_messages_produce_two_lines(self):
        """Two messages produce exactly two newline-separated lines."""
        msg1 = _make_message(
            id=1,
            username="Alice",
            content="first",
            created_at=datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
        )
        msg2 = _make_message(
            id=2,
            username="Bob",
            content="second",
            created_at=datetime(2026, 4, 1, 12, 1, tzinfo=timezone.utc),
        )
        result = format_context_messages([msg1, msg2])
        lines = result.split("\n")
        assert len(lines) == 2

    def test_message_ordering_is_preserved(self):
        """Messages appear in the order provided to format_context_messages."""
        msg1 = _make_message(id=1, username="First", content="alpha")
        msg2 = _make_message(id=2, username="Second", content="beta")
        result = format_context_messages([msg1, msg2])
        first_pos = result.index("First")
        second_pos = result.index("Second")
        assert first_pos < second_pos

    def test_mixed_bot_and_user_messages(self):
        """Bot and user messages in the same list are formatted with correct labels."""
        user_msg = _make_message(
            id=1, username="dave", content="question", is_bot=False
        )
        bot_msg = _make_message(id=2, username="bot", content="answer", is_bot=True)
        result = format_context_messages([user_msg, bot_msg])
        assert "dave: question" in result
        assert "Assistant: answer" in result


# ---------------------------------------------------------------------------
# format_context_messages — attachments
# ---------------------------------------------------------------------------


class TestFormatContextMessagesAttachments:
    def test_multiple_attachments_per_message_all_appear(self):
        """All image descriptions from multiple attachments for one message are included."""
        msg = _make_message(id=5, content="look at these")
        attachments_map = {
            5: [
                (
                    Attachment(id=1, message_id=5, blob_sha256="aaa", filename="a.png"),
                    Blob(
                        sha256="aaa",
                        content_type="image/png",
                        description="A dog",
                    ),
                ),
                (
                    Attachment(id=2, message_id=5, blob_sha256="bbb", filename="b.png"),
                    Blob(
                        sha256="bbb",
                        content_type="image/png",
                        description="A cat",
                    ),
                ),
            ]
        }
        result = format_context_messages([msg], attachments_map)
        assert "[Image: A dog]" in result
        assert "[Image: A cat]" in result

    def test_no_image_line_when_message_id_absent_from_map(self):
        """No image annotation is added when the message id is not in the attachments map."""
        msg = _make_message(id=10, content="plain message")
        attachments_map = {99: []}  # different message id
        result = format_context_messages([msg], attachments_map)
        assert "[Image:" not in result

    def test_none_attachments_map_produces_no_image_lines(self):
        """Passing attachments_by_msg=None does not raise and adds no image annotations."""
        msg = _make_message(id=3, content="no pics here")
        result = format_context_messages([msg], None)
        assert "[Image:" not in result
        assert "no pics here" in result

    def test_attachment_image_line_uses_indented_format(self):
        """Image descriptions are prefixed with two spaces and the [Image:] marker."""
        msg = _make_message(id=7, content="see this")
        attachments_map = {
            7: [
                (
                    Attachment(id=1, message_id=7, blob_sha256="ccc", filename="c.png"),
                    Blob(
                        sha256="ccc",
                        content_type="image/png",
                        description="Sunset",
                    ),
                ),
            ]
        }
        result = format_context_messages([msg], attachments_map)
        assert "  [Image: Sunset]" in result


# ===========================================================================
# _coerce_username — gaps not covered elsewhere
# ===========================================================================


class TestCoerceUsernameNoneInput:
    def test_none_returns_none(self):
        """None input produces None without error."""
        assert _coerce_username(None) is None


class TestCoerceUsernameDictKeys:
    def test_dict_with_username_key(self):
        """Dict with 'username' key returns that string value."""
        assert _coerce_username({"username": "alice"}) == "alice"

    def test_dict_with_name_key(self):
        """Dict with 'name' key (no 'username') returns name."""
        assert _coerce_username({"name": "bob"}) == "bob"

    def test_dict_with_display_name_key(self):
        """Dict with only 'display_name' returns that value."""
        assert _coerce_username({"display_name": "carol"}) == "carol"

    def test_username_key_takes_priority_over_name_and_display_name(self):
        """'username' wins when all three keys are present."""
        assert (
            _coerce_username(
                {"username": "first", "name": "second", "display_name": "third"}
            )
            == "first"
        )

    def test_name_key_takes_priority_over_display_name(self):
        """'name' wins over 'display_name' when 'username' is absent."""
        assert _coerce_username({"name": "second", "display_name": "third"}) == "second"

    def test_non_string_username_value_skipped_falls_through_to_name(self):
        """Dict 'username' with a non-string value is skipped; 'name' is returned."""
        assert _coerce_username({"username": 99, "name": "fallback"}) == "fallback"

    def test_all_known_keys_with_non_string_values_returns_none(self, caplog):
        """When all known keys exist but have non-string values, return None with warning."""
        with caplog.at_level(logging.WARNING, logger="chat.agent"):
            result = _coerce_username({"username": 1, "name": 2, "display_name": 3})
        assert result is None
        assert any("Could not extract username" in r.message for r in caplog.records)


class TestCoerceUsernameInvalidDict:
    def test_empty_dict_returns_none_with_warning(self, caplog):
        """Empty dict logs a warning and returns None."""
        with caplog.at_level(logging.WARNING, logger="chat.agent"):
            result = _coerce_username({})
        assert result is None
        assert any("Could not extract username" in r.message for r in caplog.records)

    def test_dict_with_unrelated_keys_returns_none_with_warning(self, caplog):
        """Dict without any known username keys returns None and logs a warning."""
        with caplog.at_level(logging.WARNING, logger="chat.agent"):
            result = _coerce_username({"id": 42, "email": "x@example.com"})
        assert result is None
        assert any("Could not extract username" in r.message for r in caplog.records)


class TestCoerceUsernameNonStringTypes:
    def test_integer_coerced_to_string(self):
        """Integer is str()-converted."""
        assert _coerce_username(42) == "42"

    def test_float_coerced_to_string(self):
        """Float is str()-converted."""
        assert _coerce_username(3.14) == "3.14"

    def test_list_coerced_to_string(self):
        """List (non-str, non-dict) is str()-converted."""
        assert _coerce_username([1, 2]) == "[1, 2]"

    def test_bool_coerced_to_string(self):
        """bool (subclass of int, not str) is str()-converted."""
        assert _coerce_username(True) == "True"


# ===========================================================================
# ChatDeps dataclass — all three fields
# ===========================================================================


class TestChatDepsValidation:
    def test_channel_id_field(self):
        """ChatDeps stores channel_id correctly."""
        deps = ChatDeps(
            channel_id="my-channel", store=MagicMock(), embed_client=AsyncMock()
        )
        assert deps.channel_id == "my-channel"

    def test_store_field(self):
        """ChatDeps stores the MessageStore instance under .store."""
        store = MagicMock()
        deps = ChatDeps(channel_id="ch1", store=store, embed_client=AsyncMock())
        assert deps.store is store

    def test_embed_client_field(self):
        """ChatDeps stores the EmbeddingClient instance under .embed_client."""
        embed_client = AsyncMock()
        deps = ChatDeps(channel_id="ch1", store=MagicMock(), embed_client=embed_client)
        assert deps.embed_client is embed_client

    def test_all_three_fields_accessible(self):
        """All three fields are independently readable after construction."""
        store = MagicMock()
        embed_client = AsyncMock()
        deps = ChatDeps(channel_id="xyz", store=store, embed_client=embed_client)
        assert deps.channel_id == "xyz"
        assert deps.store is store
        assert deps.embed_client is embed_client


# ===========================================================================
# web_search tool
# ===========================================================================


class TestWebSearchTool:
    @pytest.mark.asyncio
    async def test_passes_query_to_search_web(self):
        """web_search tool forwards the query string to search_web()."""
        calls: list[str] = []

        async def fake_search(query: str, base_url: str | None = None) -> str:
            calls.append(query)
            return "results"

        agent = create_agent(base_url="http://fake:8080")
        with patch("chat.agent.search_web", side_effect=fake_search):
            await agent.run(
                "p",
                model=_tool_model("web_search", {"query": "climate news"}),
            )

        assert calls == ["climate news"]

    @pytest.mark.asyncio
    async def test_returns_search_web_result_verbatim(self):
        """web_search tool returns exactly what search_web() returns to the agent."""

        async def fake_search(query: str, base_url: str | None = None) -> str:
            return "mocked result text"

        captured: list[str] = []
        agent = create_agent(base_url="http://fake:8080")
        with patch("chat.agent.search_web", side_effect=fake_search):
            await agent.run(
                "p",
                model=_capturing_model("web_search", {"query": "q"}, captured),
            )

        assert captured == ["mocked result text"]


# ===========================================================================
# search_history tool
# ===========================================================================


class TestSearchHistoryQueryEmbedding:
    @pytest.mark.asyncio
    async def test_embeds_the_query_string(self):
        """search_history calls embed_client.embed() with the provided query."""
        embed_client = AsyncMock()
        embed_client.embed.return_value = [0.1] * 1024
        store = MagicMock()
        store.find_user_id_by_username.return_value = None
        store.search_similar.return_value = []

        deps = _make_deps(store=store, embed_client=embed_client)
        await create_agent(base_url="http://fake:8080").run(
            "p",
            model=_tool_model(
                "search_history",
                {"query": "deployment logs", "username": None, "limit": 5},
            ),
            deps=deps,
        )

        embed_client.embed.assert_called_once_with("deployment logs")

    @pytest.mark.asyncio
    async def test_passes_embedding_and_channel_to_search_similar(self):
        """search_history forwards embedding and channel_id to store.search_similar."""
        embedding = [0.7] * 1024
        embed_client = AsyncMock()
        embed_client.embed.return_value = embedding
        store = MagicMock()
        store.find_user_id_by_username.return_value = None
        store.search_similar.return_value = []

        deps = _make_deps(store=store, embed_client=embed_client, channel_id="chan-x")
        await create_agent(base_url="http://fake:8080").run(
            "p",
            model=_tool_model(
                "search_history", {"query": "foo", "username": None, "limit": 5}
            ),
            deps=deps,
        )

        kw = store.search_similar.call_args.kwargs
        assert kw["query_embedding"] == embedding
        assert kw["channel_id"] == "chan-x"

    @pytest.mark.asyncio
    async def test_no_results_returns_sentinel(self):
        """search_history returns 'No matching messages found.' when store is empty."""
        embed_client = AsyncMock()
        embed_client.embed.return_value = [0.0] * 1024
        store = MagicMock()
        store.find_user_id_by_username.return_value = None
        store.search_similar.return_value = []

        captured: list[str] = []
        deps = _make_deps(store=store, embed_client=embed_client)
        await create_agent(base_url="http://fake:8080").run(
            "p",
            model=_capturing_model(
                "search_history", {"query": "q", "username": None, "limit": 5}, captured
            ),
            deps=deps,
        )

        assert captured == ["No matching messages found."]


class TestSearchHistoryUsernameCoercion:
    @pytest.mark.asyncio
    async def test_none_username_skips_user_lookup(self):
        """search_history does NOT call find_user_id_by_username when username is None."""
        embed_client = AsyncMock()
        embed_client.embed.return_value = [0.0] * 1024
        store = MagicMock()
        store.search_similar.return_value = []

        deps = _make_deps(store=store, embed_client=embed_client)
        await create_agent(base_url="http://fake:8080").run(
            "p",
            model=_tool_model(
                "search_history", {"query": "q", "username": None, "limit": 5}
            ),
            deps=deps,
        )

        store.find_user_id_by_username.assert_not_called()

    @pytest.mark.asyncio
    async def test_string_username_triggers_user_lookup(self):
        """A plain string username causes find_user_id_by_username to be called."""
        embed_client = AsyncMock()
        embed_client.embed.return_value = [0.0] * 1024
        store = MagicMock()
        store.find_user_id_by_username.return_value = "uid-alice"
        store.search_similar.return_value = []

        deps = _make_deps(store=store, embed_client=embed_client, channel_id="ch-t")
        await create_agent(base_url="http://fake:8080").run(
            "p",
            model=_tool_model(
                "search_history", {"query": "q", "username": "alice", "limit": 5}
            ),
            deps=deps,
        )

        store.find_user_id_by_username.assert_called_once_with("ch-t", "alice")

    @pytest.mark.asyncio
    async def test_dict_username_coerced_and_looked_up(self):
        """search_history extracts the username from a dict and resolves it via the store."""
        embed_client = AsyncMock()
        embed_client.embed.return_value = [0.0] * 1024
        store = MagicMock()
        store.find_user_id_by_username.return_value = "uid-charlie"
        store.search_similar.return_value = []

        deps = _make_deps(store=store, embed_client=embed_client, channel_id="ch-dict")
        await create_agent(base_url="http://fake:8080").run(
            "p",
            model=_tool_model(
                "search_history",
                {
                    "query": "q",
                    "username": {"username": "charlie", "id": 7},
                    "limit": 5,
                },
            ),
            deps=deps,
        )

        store.find_user_id_by_username.assert_called_once_with("ch-dict", "charlie")

    @pytest.mark.asyncio
    async def test_unresolvable_dict_username_passes_none_user_id(self):
        """An unrecognised dict username coerces to None; user_id=None is used."""
        embed_client = AsyncMock()
        embed_client.embed.return_value = [0.0] * 1024
        store = MagicMock()
        store.search_similar.return_value = []

        deps = _make_deps(store=store, embed_client=embed_client)
        await create_agent(base_url="http://fake:8080").run(
            "p",
            model=_tool_model(
                "search_history",
                {"query": "q", "username": {"id": 99, "email": "x@y.com"}, "limit": 5},
            ),
            deps=deps,
        )

        store.find_user_id_by_username.assert_not_called()
        assert store.search_similar.call_args.kwargs.get("user_id") is None

    @pytest.mark.asyncio
    async def test_discord_user_id_mention_dict_used_directly(self):
        """A {'type': 'user_id', 'id': ...} dict bypasses username lookup and uses the id directly."""
        embed_client = AsyncMock()
        embed_client.embed.return_value = [0.0] * 1024
        store = MagicMock()
        store.search_similar.return_value = []

        deps = _make_deps(
            store=store, embed_client=embed_client, channel_id="ch-mention"
        )
        await create_agent(base_url="http://fake:8080").run(
            "p",
            model=_tool_model(
                "search_history",
                {
                    "query": "changelog",
                    "username": {"type": "user_id", "id": "1294788769672593501"},
                    "limit": 5,
                },
            ),
            deps=deps,
        )

        store.find_user_id_by_username.assert_not_called()
        assert store.search_similar.call_args.kwargs["user_id"] == "1294788769672593501"

    @pytest.mark.asyncio
    async def test_resolved_user_id_forwarded_to_search_similar(self):
        """Resolved user_id from store lookup is passed to store.search_similar."""
        embed_client = AsyncMock()
        embed_client.embed.return_value = [0.0] * 1024
        store = MagicMock()
        store.find_user_id_by_username.return_value = "uid-found"
        store.search_similar.return_value = []

        deps = _make_deps(store=store, embed_client=embed_client)
        await create_agent(base_url="http://fake:8080").run(
            "p",
            model=_tool_model(
                "search_history", {"query": "q", "username": "alice", "limit": 5}
            ),
            deps=deps,
        )

        assert store.search_similar.call_args.kwargs["user_id"] == "uid-found"


class TestSearchHistoryLimit:
    @pytest.mark.asyncio
    async def test_limit_above_20_is_clamped(self):
        """A limit > 20 is clamped to 20 when calling store.search_similar."""
        embed_client = AsyncMock()
        embed_client.embed.return_value = [0.0] * 1024
        store = MagicMock()
        store.find_user_id_by_username.return_value = None
        store.search_similar.return_value = []

        deps = _make_deps(store=store, embed_client=embed_client)
        await create_agent(base_url="http://fake:8080").run(
            "p",
            model=_tool_model(
                "search_history", {"query": "q", "username": None, "limit": 50}
            ),
            deps=deps,
        )

        assert store.search_similar.call_args.kwargs["limit"] == 20

    @pytest.mark.asyncio
    async def test_limit_exactly_20_not_capped_further(self):
        """A limit of exactly 20 is passed through unchanged (boundary condition)."""
        embed_client = AsyncMock()
        embed_client.embed.return_value = [0.0] * 1024
        store = MagicMock()
        store.find_user_id_by_username.return_value = None
        store.search_similar.return_value = []

        deps = _make_deps(store=store, embed_client=embed_client)
        await create_agent(base_url="http://fake:8080").run(
            "p",
            model=_tool_model(
                "search_history", {"query": "q", "username": None, "limit": 20}
            ),
            deps=deps,
        )

        assert store.search_similar.call_args.kwargs["limit"] == 20

    @pytest.mark.asyncio
    async def test_limit_below_20_passed_unchanged(self):
        """A limit below 20 is passed as-is to store.search_similar."""
        embed_client = AsyncMock()
        embed_client.embed.return_value = [0.0] * 1024
        store = MagicMock()
        store.find_user_id_by_username.return_value = None
        store.search_similar.return_value = []

        deps = _make_deps(store=store, embed_client=embed_client)
        await create_agent(base_url="http://fake:8080").run(
            "p",
            model=_tool_model(
                "search_history", {"query": "q", "username": None, "limit": 3}
            ),
            deps=deps,
        )

        assert store.search_similar.call_args.kwargs["limit"] == 3


# ===========================================================================
# get_user_summary tool
# ===========================================================================


class TestGetUserSummaryListPath:
    @pytest.mark.asyncio
    async def test_no_username_calls_list_user_summaries_with_channel(self):
        """No username → list_user_summaries() called with the correct channel_id."""
        store = MagicMock()
        store.list_user_summaries.return_value = []

        deps = _make_deps(store=store, channel_id="list-chan")
        await create_agent(base_url="http://fake:8080").run(
            "p",
            model=_tool_model("get_user_summary", {}),
            deps=deps,
        )

        store.list_user_summaries.assert_called_once_with("list-chan")

    @pytest.mark.asyncio
    async def test_empty_list_returns_no_summaries_sentinel(self):
        """Empty summaries list returns the 'No user summaries available' message."""
        store = MagicMock()
        store.list_user_summaries.return_value = []

        captured: list[str] = []
        deps = _make_deps(store=store)
        await create_agent(base_url="http://fake:8080").run(
            "p",
            model=_capturing_model("get_user_summary", {}, captured),
            deps=deps,
        )

        assert "No user summaries available" in captured[0]

    @pytest.mark.asyncio
    async def test_list_includes_all_usernames_and_count(self):
        """List-mode output includes every username and the total count."""
        store = MagicMock()
        store.list_user_summaries.return_value = [
            _make_summary(username="alice"),
            _make_summary(username="bob"),
            _make_summary(username="carol"),
        ]

        captured: list[str] = []
        deps = _make_deps(store=store)
        await create_agent(base_url="http://fake:8080").run(
            "p",
            model=_capturing_model("get_user_summary", {}, captured),
            deps=deps,
        )

        assert "alice" in captured[0]
        assert "bob" in captured[0]
        assert "carol" in captured[0]
        assert "3" in captured[0]

    @pytest.mark.asyncio
    async def test_list_includes_updated_date_for_each_user(self):
        """List-mode output includes the formatted last-updated date."""
        store = MagicMock()
        store.list_user_summaries.return_value = [
            _make_summary(
                username="dave", updated=datetime(2026, 3, 15, tzinfo=timezone.utc)
            )
        ]

        captured: list[str] = []
        deps = _make_deps(store=store)
        await create_agent(base_url="http://fake:8080").run(
            "p",
            model=_capturing_model("get_user_summary", {}, captured),
            deps=deps,
        )

        assert "2026-03-15" in captured[0]


class TestGetUserSummarySpecificUserPath:
    @pytest.mark.asyncio
    async def test_string_username_calls_get_user_summary_with_channel_and_name(self):
        """Providing a username calls store.get_user_summary(channel_id, username)."""
        store = MagicMock()
        store.get_user_summary.return_value = _make_summary(username="frank")

        deps = _make_deps(store=store, channel_id="spec-chan")
        await create_agent(base_url="http://fake:8080").run(
            "p",
            model=_tool_model("get_user_summary", {"username": "frank"}),
            deps=deps,
        )

        store.get_user_summary.assert_called_once_with("spec-chan", "frank")

    @pytest.mark.asyncio
    async def test_found_summary_returns_formatted_text(self):
        """A found summary includes username, date, and summary body text."""
        store = MagicMock()
        store.get_user_summary.return_value = _make_summary(
            username="grace",
            summary_text="Grace discussed Python and testing.",
            updated=datetime(2026, 4, 2, tzinfo=timezone.utc),
        )

        captured: list[str] = []
        deps = _make_deps(store=store)
        await create_agent(base_url="http://fake:8080").run(
            "p",
            model=_capturing_model("get_user_summary", {"username": "grace"}, captured),
            deps=deps,
        )

        assert "grace" in captured[0]
        assert "Python and testing" in captured[0]
        assert "2026-04-02" in captured[0]

    @pytest.mark.asyncio
    async def test_not_found_returns_no_summary_sentinel(self):
        """Missing summary returns 'No summary available for <username>.'."""
        store = MagicMock()
        store.get_user_summary.return_value = None

        captured: list[str] = []
        deps = _make_deps(store=store)
        await create_agent(base_url="http://fake:8080").run(
            "p",
            model=_capturing_model("get_user_summary", {"username": "henry"}, captured),
            deps=deps,
        )

        assert "No summary available for henry" in captured[0]

    @pytest.mark.asyncio
    async def test_dict_username_coerced_to_string_before_store_lookup(self):
        """A dict username with 'username' key is resolved to a string before store lookup."""
        store = MagicMock()
        store.get_user_summary.return_value = _make_summary(username="ivan")

        deps = _make_deps(store=store, channel_id="dict-chan")
        await create_agent(base_url="http://fake:8080").run(
            "p",
            model=_tool_model(
                "get_user_summary", {"username": {"username": "ivan", "id": 5}}
            ),
            deps=deps,
        )

        store.get_user_summary.assert_called_once_with("dict-chan", "ivan")

    @pytest.mark.asyncio
    async def test_unresolvable_dict_username_falls_back_to_list_mode(self):
        """An unrecognised dict username coerces to None, triggering list mode."""
        store = MagicMock()
        store.list_user_summaries.return_value = []

        deps = _make_deps(store=store)
        await create_agent(base_url="http://fake:8080").run(
            "p",
            model=_tool_model(
                "get_user_summary", {"username": {"id": 99, "email": "x@y.com"}}
            ),
            deps=deps,
        )

        store.list_user_summaries.assert_called_once()
        store.get_user_summary.assert_not_called()


# ===========================================================================
# build_system_prompt — persona, DO/DON'T structure, and behavioral guidance
# ===========================================================================


class TestBuildSystemPromptPersona:
    def test_describes_friend_persona(self):
        """System prompt uses 'friend hanging out' persona, not 'helpful assistant'."""
        prompt = build_system_prompt()
        assert "friend hanging out" in prompt

    def test_does_not_describe_helpful_assistant(self):
        """System prompt does NOT describe the agent as a 'helpful assistant'."""
        prompt = build_system_prompt()
        assert "helpful assistant" not in prompt

    def test_mentions_casual_natural_tone(self):
        """System prompt describes a casual, direct, and natural conversation style."""
        prompt = build_system_prompt()
        assert "casual" in prompt


class TestBuildSystemPromptDoSectionHeader:
    def test_do_section_header_present(self):
        """System prompt contains a 'DO:' section header."""
        prompt = build_system_prompt()
        assert "DO:" in prompt

    def test_dont_section_header_present(self):
        """System prompt contains a \"DON'T:\" section header."""
        prompt = build_system_prompt()
        assert "DON'T:" in prompt

    def test_do_header_appears_before_dont_header(self):
        """The DO: section comes before the DON'T: section in the prompt."""
        prompt = build_system_prompt()
        do_pos = prompt.index("DO:")
        dont_pos = prompt.index("DON'T:")
        assert do_pos < dont_pos


class TestBuildSystemPromptDoSection:
    def test_instructs_answer_directly(self):
        """DO section tells the agent to answer directly."""
        prompt = build_system_prompt()
        assert "Answer directly" in prompt

    def test_instructs_match_the_vibe(self):
        """DO section tells the agent to match the vibe of the conversation."""
        prompt = build_system_prompt()
        assert "Match the vibe" in prompt

    def test_instructs_search_first_behavior(self):
        """DO section explicitly says to search before responding."""
        prompt = build_system_prompt()
        assert "Search before you respond" in prompt

    def test_instructs_when_in_doubt_search(self):
        """DO section ends the search directive with 'When in doubt, search'."""
        prompt = build_system_prompt()
        assert "When in doubt, search" in prompt

    def test_instructs_short_casual_replies(self):
        """READ THE ROOM section keeps casual chatter short (a sentence or two)."""
        prompt = build_system_prompt()
        assert "a sentence or two" in prompt.lower()


class TestBuildSystemPromptDontSection:
    def test_prohibits_contextual_narration(self):
        """DON'T section forbids narrating with 'contextually, they are referring to'."""
        prompt = build_system_prompt()
        assert "contextually, they are referring to" in prompt

    def test_prohibits_padding_small_talk_into_essay(self):
        """DON'T section forbids padding casual chat into an essay."""
        prompt = build_system_prompt()
        assert "essay" in prompt

    def test_prohibits_sure_filler_starter(self):
        """DON'T section lists 'Sure!' as a prohibited filler starter."""
        prompt = build_system_prompt()
        assert "Sure!" in prompt

    def test_prohibits_of_course_filler_starter(self):
        """DON'T section lists 'Of course!' as a prohibited filler starter."""
        prompt = build_system_prompt()
        assert "Of course!" in prompt

    def test_prohibits_great_question_filler_starter(self):
        """DON'T section lists 'Great question!' as a prohibited filler starter."""
        prompt = build_system_prompt()
        assert "Great question!" in prompt

    def test_prohibits_announcing_tool_usage(self):
        """DON'T section tells the agent not to announce that it is using a tool."""
        prompt = build_system_prompt()
        # The prompt says "Announce that you're using a tool"
        assert "Announce" in prompt

    def test_prohibits_as_an_ai_apology(self):
        """DON'T section forbids saying 'as an AI'."""
        prompt = build_system_prompt()
        assert "as an AI" in prompt
