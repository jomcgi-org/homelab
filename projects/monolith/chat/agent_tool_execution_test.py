"""Tests for search_history and get_user_summary tool bodies via FunctionModel.

These tests drive the actual tool execution paths (not just registration)
using pydantic_ai's FunctionModel, which lets us control what the LLM
"requests" and then verify what the tool returns.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from chat.agent import ChatDeps, create_agent


def _make_deps(
    store: MagicMock,
    embed_client: AsyncMock,
    channel_id: str = "ch1",
) -> ChatDeps:
    return ChatDeps(channel_id=channel_id, store=store, embed_client=embed_client)


def _tool_once_then_done(tool_name: str, args: dict) -> object:
    """Return a FunctionModel function that calls one tool then returns 'done'."""

    def model_func(messages, info):  # type: ignore[type-arg]
        for msg in messages:
            if hasattr(msg, "parts"):
                for part in msg.parts:
                    if isinstance(part, ToolReturnPart):
                        return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name=tool_name,
                    args=args,
                    tool_call_id="call-1",
                )
            ]
        )

    return FunctionModel(model_func)


# ---------------------------------------------------------------------------
# search_history — "No matching messages found." branch
# ---------------------------------------------------------------------------


class TestSearchHistoryNoResults:
    @pytest.mark.asyncio
    async def test_returns_no_matching_messages_when_empty(self):
        """search_history returns 'No matching messages found.' when store returns []."""
        embed_client = AsyncMock()
        embed_client.embed.return_value = [0.1] * 1024

        store = MagicMock()
        store.find_user_id_by_username.return_value = None
        store.search_hybrid.return_value = []  # no results

        deps = _make_deps(store, embed_client)
        agent = create_agent(base_url="http://fake:8080")

        tool_return_captured = []

        def model_func(messages, info):  # type: ignore[type-arg]
            for msg in messages:
                if hasattr(msg, "parts"):
                    for part in msg.parts:
                        if isinstance(part, ToolReturnPart):
                            tool_return_captured.append(part.content)
                            return ModelResponse(parts=[TextPart("done")])
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="search_history",
                        args={"query": "weather", "username": None, "limit": 5},
                        tool_call_id="call-1",
                    )
                ]
            )

        await agent.run(
            "What was said about weather?",
            model=FunctionModel(model_func),
            deps=deps,
        )

        assert len(tool_return_captured) == 1
        assert tool_return_captured[0] == "No matching messages found."

    @pytest.mark.asyncio
    async def test_embed_client_called_with_query(self):
        """search_history calls embed_client.embed() with the search query."""
        embed_client = AsyncMock()
        embed_client.embed.return_value = [0.0] * 1024

        store = MagicMock()
        store.find_user_id_by_username.return_value = None
        store.search_hybrid.return_value = []

        deps = _make_deps(store, embed_client)
        agent = create_agent(base_url="http://fake:8080")

        await agent.run(
            "search prompt",
            model=_tool_once_then_done(
                "search_history",
                {"query": "python deploy", "username": None, "limit": 5},
            ),
            deps=deps,
        )

        embed_client.embed.assert_called_once_with("python deploy")

    @pytest.mark.asyncio
    async def test_search_hybrid_called_with_channel_id_and_embedding(self):
        """search_history passes channel_id and query_embedding to store.search_hybrid."""
        embedding = [0.5] * 1024
        embed_client = AsyncMock()
        embed_client.embed.return_value = embedding

        store = MagicMock()
        store.find_user_id_by_username.return_value = None
        store.search_hybrid.return_value = []

        deps = _make_deps(store, embed_client, channel_id="my-channel")
        agent = create_agent(base_url="http://fake:8080")

        await agent.run(
            "search prompt",
            model=_tool_once_then_done(
                "search_history",
                {"query": "hello", "username": None, "limit": 5},
            ),
            deps=deps,
        )

        store.search_hybrid.assert_called_once()
        call_kwargs = store.search_hybrid.call_args
        assert call_kwargs.kwargs.get("channel_id") == "my-channel"
        assert call_kwargs.kwargs.get("query_embedding") == embedding


# ---------------------------------------------------------------------------
# search_history — with results (calls format_context_messages)
# ---------------------------------------------------------------------------


class TestSearchHistoryWithResults:
    @pytest.mark.asyncio
    async def test_returns_formatted_messages_when_results_exist(self):
        """search_history returns formatted context when store returns messages."""

        from chat.models import Message

        embed_client = AsyncMock()
        embed_client.embed.return_value = [0.0] * 1024

        found_msg = Message(
            id=1,
            discord_message_id="111",
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            content="the weather is nice",
            is_bot=False,
            embedding=[0.0] * 1024,
            created_at=datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
        )

        store = MagicMock()
        store.find_user_id_by_username.return_value = None
        store.search_hybrid.return_value = [found_msg]

        deps = _make_deps(store, embed_client)
        agent = create_agent(base_url="http://fake:8080")

        tool_return_captured = []

        def model_func(messages, info):  # type: ignore[type-arg]
            for msg in messages:
                if hasattr(msg, "parts"):
                    for part in msg.parts:
                        if isinstance(part, ToolReturnPart):
                            tool_return_captured.append(part.content)
                            return ModelResponse(parts=[TextPart("done")])
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="search_history",
                        args={"query": "weather", "username": None, "limit": 5},
                        tool_call_id="call-1",
                    )
                ]
            )

        await agent.run(
            "What was said about weather?",
            model=FunctionModel(model_func),
            deps=deps,
        )

        assert len(tool_return_captured) == 1
        assert "Alice" in tool_return_captured[0]
        assert "the weather is nice" in tool_return_captured[0]


# ---------------------------------------------------------------------------
# search_history — username filter (calls find_user_id_by_username)
# ---------------------------------------------------------------------------


class TestSearchHistoryWithUsername:
    @pytest.mark.asyncio
    async def test_looks_up_user_id_when_username_provided(self):
        """search_history calls find_user_id_by_username when a username is given."""
        embed_client = AsyncMock()
        embed_client.embed.return_value = [0.0] * 1024

        store = MagicMock()
        store.find_user_id_by_username.return_value = "u-alice"
        store.search_hybrid.return_value = []

        deps = _make_deps(store, embed_client)
        agent = create_agent(base_url="http://fake:8080")

        await agent.run(
            "search by user",
            model=_tool_once_then_done(
                "search_history",
                {"query": "topic", "username": "alice", "limit": 5},
            ),
            deps=deps,
        )

        store.find_user_id_by_username.assert_called_once_with("ch1", "alice")

    @pytest.mark.asyncio
    async def test_passes_user_id_to_search_hybrid(self):
        """search_history forwards the resolved user_id to store.search_hybrid."""
        embed_client = AsyncMock()
        embed_client.embed.return_value = [0.0] * 1024

        store = MagicMock()
        store.find_user_id_by_username.return_value = "u-alice"
        store.search_hybrid.return_value = []

        deps = _make_deps(store, embed_client)
        agent = create_agent(base_url="http://fake:8080")

        await agent.run(
            "search by user",
            model=_tool_once_then_done(
                "search_history",
                {"query": "topic", "username": "alice", "limit": 5},
            ),
            deps=deps,
        )

        call_kwargs = store.search_hybrid.call_args.kwargs
        assert call_kwargs.get("user_id") == "u-alice"

    @pytest.mark.asyncio
    async def test_limit_is_capped_at_20(self):
        """search_history clamps the limit to min(requested, 20)."""
        embed_client = AsyncMock()
        embed_client.embed.return_value = [0.0] * 1024

        store = MagicMock()
        store.find_user_id_by_username.return_value = None
        store.search_hybrid.return_value = []

        deps = _make_deps(store, embed_client)
        agent = create_agent(base_url="http://fake:8080")

        await agent.run(
            "search with huge limit",
            model=_tool_once_then_done(
                "search_history",
                {"query": "topic", "username": None, "limit": 100},
            ),
            deps=deps,
        )

        call_kwargs = store.search_hybrid.call_args.kwargs
        assert call_kwargs.get("limit") == 20


# ---------------------------------------------------------------------------
# get_user_summary — list mode (username=None)
# ---------------------------------------------------------------------------


class TestGetUserSummaryListMode:
    @pytest.mark.asyncio
    async def test_returns_user_list_when_no_username(self):
        """get_user_summary returns a list of users when called with no username."""

        from chat.models import UserChannelSummary

        embed_client = AsyncMock()
        store = MagicMock()
        store.list_user_summaries.return_value = [
            UserChannelSummary(
                channel_id="ch1",
                user_id="u1",
                username="Alice",
                summary="Alice summary.",
                last_message_id=10,
                updated_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
            ),
            UserChannelSummary(
                channel_id="ch1",
                user_id="u2",
                username="Bob",
                summary="Bob summary.",
                last_message_id=20,
                updated_at=datetime(2026, 3, 15, tzinfo=timezone.utc),
            ),
        ]

        deps = _make_deps(store, embed_client)
        agent = create_agent(base_url="http://fake:8080")

        tool_return_captured = []

        def model_func(messages, info):  # type: ignore[type-arg]
            for msg in messages:
                if hasattr(msg, "parts"):
                    for part in msg.parts:
                        if isinstance(part, ToolReturnPart):
                            tool_return_captured.append(part.content)
                            return ModelResponse(parts=[TextPart("done")])
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="get_user_summary",
                        args={},
                        tool_call_id="call-1",
                    )
                ]
            )

        await agent.run(
            "What user summaries do you have?",
            model=FunctionModel(model_func),
            deps=deps,
        )

        assert len(tool_return_captured) == 1
        result = tool_return_captured[0]
        assert "Alice" in result
        assert "Bob" in result
        assert "2" in result  # count

    @pytest.mark.asyncio
    async def test_returns_no_summaries_message_when_empty(self):
        """get_user_summary returns empty message when no summaries exist."""
        embed_client = AsyncMock()
        store = MagicMock()
        store.list_user_summaries.return_value = []

        deps = _make_deps(store, embed_client)
        agent = create_agent(base_url="http://fake:8080")

        tool_return_captured = []

        def model_func(messages, info):  # type: ignore[type-arg]
            for msg in messages:
                if hasattr(msg, "parts"):
                    for part in msg.parts:
                        if isinstance(part, ToolReturnPart):
                            tool_return_captured.append(part.content)
                            return ModelResponse(parts=[TextPart("done")])
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="get_user_summary",
                        args={},
                        tool_call_id="call-1",
                    )
                ]
            )

        await agent.run(
            "What user summaries do you have?",
            model=FunctionModel(model_func),
            deps=deps,
        )

        assert len(tool_return_captured) == 1
        assert "No user summaries available" in tool_return_captured[0]

    @pytest.mark.asyncio
    async def test_calls_list_user_summaries_with_channel(self):
        """get_user_summary passes channel_id to store.list_user_summaries."""
        embed_client = AsyncMock()
        store = MagicMock()
        store.list_user_summaries.return_value = []

        deps = _make_deps(store, embed_client, channel_id="my-chan")
        agent = create_agent(base_url="http://fake:8080")

        await agent.run(
            "List summaries",
            model=_tool_once_then_done("get_user_summary", {}),
            deps=deps,
        )

        store.list_user_summaries.assert_called_once_with("my-chan")


# ---------------------------------------------------------------------------
# get_user_summary — bad username (dict with no known key)
# ---------------------------------------------------------------------------


class TestGetUserSummaryBadUsername:
    @pytest.mark.asyncio
    async def test_falls_back_to_list_when_username_coerces_to_none(self):
        """get_user_summary falls back to list mode when username is an unrecognized dict."""
        embed_client = AsyncMock()
        store = MagicMock()
        store.list_user_summaries.return_value = []

        deps = _make_deps(store, embed_client)
        agent = create_agent(base_url="http://fake:8080")

        tool_return_captured = []

        def model_func(messages, info):  # type: ignore[type-arg]
            for msg in messages:
                if hasattr(msg, "parts"):
                    for part in msg.parts:
                        if isinstance(part, ToolReturnPart):
                            tool_return_captured.append(part.content)
                            return ModelResponse(parts=[TextPart("done")])
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="get_user_summary",
                        args={"username": {"id": 42, "email": "x@example.com"}},
                        tool_call_id="call-1",
                    )
                ]
            )

        await agent.run(
            "Who is user 42?",
            model=FunctionModel(model_func),
            deps=deps,
        )

        assert len(tool_return_captured) == 1
        # Now falls back to list mode instead of returning an error
        store.list_user_summaries.assert_called_once()
        store.get_user_summary.assert_not_called()


# ---------------------------------------------------------------------------
# get_user_summary — "No summary available" branch
# ---------------------------------------------------------------------------


class TestGetUserSummaryNotFound:
    @pytest.mark.asyncio
    async def test_returns_no_summary_message_when_store_returns_none(self):
        """get_user_summary returns 'No summary available for X.' when store has nothing."""
        embed_client = AsyncMock()
        store = MagicMock()
        store.get_user_summary.return_value = None

        deps = _make_deps(store, embed_client)
        agent = create_agent(base_url="http://fake:8080")

        tool_return_captured = []

        def model_func(messages, info):  # type: ignore[type-arg]
            for msg in messages:
                if hasattr(msg, "parts"):
                    for part in msg.parts:
                        if isinstance(part, ToolReturnPart):
                            tool_return_captured.append(part.content)
                            return ModelResponse(parts=[TextPart("done")])
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="get_user_summary",
                        args={"username": "bob"},
                        tool_call_id="call-1",
                    )
                ]
            )

        await agent.run(
            "Tell me about bob",
            model=FunctionModel(model_func),
            deps=deps,
        )

        assert len(tool_return_captured) == 1
        assert "No summary available for bob" in tool_return_captured[0]


# ---------------------------------------------------------------------------
# get_user_summary — happy path (summary exists)
# ---------------------------------------------------------------------------


class TestGetUserSummaryFound:
    @pytest.mark.asyncio
    async def test_returns_formatted_summary_when_found(self):
        """get_user_summary returns a formatted string with username and summary text."""

        from chat.models import UserChannelSummary

        embed_client = AsyncMock()
        store = MagicMock()

        summary_obj = UserChannelSummary(
            channel_id="ch1",
            user_id="u-bob",
            username="bob",
            summary="Bob has been discussing Python and deployments.",
            last_message_id=10,
            updated_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )
        store.get_user_summary.return_value = summary_obj

        deps = _make_deps(store, embed_client)
        agent = create_agent(base_url="http://fake:8080")

        tool_return_captured = []

        def model_func(messages, info):  # type: ignore[type-arg]
            for msg in messages:
                if hasattr(msg, "parts"):
                    for part in msg.parts:
                        if isinstance(part, ToolReturnPart):
                            tool_return_captured.append(part.content)
                            return ModelResponse(parts=[TextPart("done")])
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="get_user_summary",
                        args={"username": "bob"},
                        tool_call_id="call-1",
                    )
                ]
            )

        await agent.run(
            "Tell me about bob",
            model=FunctionModel(model_func),
            deps=deps,
        )

        assert len(tool_return_captured) == 1
        result = tool_return_captured[0]
        assert "bob" in result
        assert "Python and deployments" in result
        # Should include the formatted date
        assert "2026-04-01" in result

    @pytest.mark.asyncio
    async def test_get_user_summary_called_with_channel_and_username(self):
        """get_user_summary passes channel_id and username to store.get_user_summary."""

        from chat.models import UserChannelSummary

        embed_client = AsyncMock()
        store = MagicMock()

        summary_obj = UserChannelSummary(
            channel_id="ch1",
            user_id="u-alice",
            username="alice",
            summary="Alice talked about deployments.",
            last_message_id=5,
            updated_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )
        store.get_user_summary.return_value = summary_obj

        deps = _make_deps(store, embed_client, channel_id="my-chan")
        agent = create_agent(base_url="http://fake:8080")

        await agent.run(
            "Who is alice?",
            model=_tool_once_then_done("get_user_summary", {"username": "alice"}),
            deps=deps,
        )

        store.get_user_summary.assert_called_once_with("my-chan", "alice")


# ---------------------------------------------------------------------------
# get_user_summary — Discord @-mention dict resolves by user_id
# ---------------------------------------------------------------------------


class TestGetUserSummaryMention:
    @pytest.mark.asyncio
    async def test_mention_dict_resolves_summary_by_user_id(self):
        """A {'type': 'user_id'} mention is looked up by user_id, not username."""

        from chat.models import UserChannelSummary

        embed_client = AsyncMock()
        store = MagicMock()

        summary_obj = UserChannelSummary(
            channel_id="ch1",
            user_id="98146497454960640",
            username="𝔲𝔤𝔩𝔶𝔟𝔬𝔶",
            summary="uglyboy critiques tech ecosystems through cynical pragmatism.",
            last_message_id=10,
            updated_at=datetime(2026, 5, 27, tzinfo=timezone.utc),
        )
        store.get_user_summary_by_user_id.return_value = summary_obj

        deps = _make_deps(store, embed_client)
        agent = create_agent(base_url="http://fake:8080")

        tool_return_captured = []

        def model_func(messages, info):  # type: ignore[type-arg]
            for msg in messages:
                if hasattr(msg, "parts"):
                    for part in msg.parts:
                        if isinstance(part, ToolReturnPart):
                            tool_return_captured.append(part.content)
                            return ModelResponse(parts=[TextPart("done")])
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="get_user_summary",
                        args={
                            "username": {
                                "type": "user_id",
                                "id": "98146497454960640",
                            }
                        },
                        tool_call_id="call-1",
                    )
                ]
            )

        await agent.run(
            "what do your notes say about @uglyboy",
            model=FunctionModel(model_func),
            deps=deps,
        )

        # Resolved by the stable id, never by the mutable display name.
        store.get_user_summary_by_user_id.assert_called_once_with(
            "ch1", "98146497454960640"
        )
        store.get_user_summary.assert_not_called()
        store.list_user_summaries.assert_not_called()
        assert len(tool_return_captured) == 1
        result = tool_return_captured[0]
        assert "critiques tech ecosystems" in result
        assert "𝔲𝔤𝔩𝔶𝔟𝔬𝔶" in result

    @pytest.mark.asyncio
    async def test_mention_dict_reports_no_summary_when_absent(self):
        """An unknown mentioned user_id returns a 'no summary' message, not a list."""
        embed_client = AsyncMock()
        store = MagicMock()
        store.get_user_summary_by_user_id.return_value = None

        deps = _make_deps(store, embed_client)
        agent = create_agent(base_url="http://fake:8080")

        tool_return_captured = []

        def model_func(messages, info):  # type: ignore[type-arg]
            for msg in messages:
                if hasattr(msg, "parts"):
                    for part in msg.parts:
                        if isinstance(part, ToolReturnPart):
                            tool_return_captured.append(part.content)
                            return ModelResponse(parts=[TextPart("done")])
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="get_user_summary",
                        args={"username": {"type": "user_id", "id": "404"}},
                        tool_call_id="call-1",
                    )
                ]
            )

        await agent.run(
            "what about @nobody",
            model=FunctionModel(model_func),
            deps=deps,
        )

        store.get_user_summary_by_user_id.assert_called_once_with("ch1", "404")
        store.list_user_summaries.assert_not_called()
        assert len(tool_return_captured) == 1
        assert "404" in tool_return_captured[0]
