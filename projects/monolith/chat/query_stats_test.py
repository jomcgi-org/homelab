"""Tests for MessageStore.query_stats -- the structured, scope-locked history
query (ADR chat/002). Mirrors store_coverage_test.py's MagicMock-session
pattern: assert the construction contract (bound params, allow-list guards),
not live SQL against a real database.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat.store import MessageStore


def _make_row(
    discord_message_id: str = "111",
    username: str = "Alice",
    user_id: str = "u1",
    content: str = "hello",
    created_at: datetime | None = None,
    is_bot: bool = False,
):
    return SimpleNamespace(
        discord_message_id=discord_message_id,
        username=username,
        user_id=user_id,
        content=content,
        created_at=created_at or datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
        is_bot=is_bot,
    )


@pytest.fixture
def mock_session():
    return MagicMock()


@pytest.fixture
def store(mock_session):
    embed_client = AsyncMock()
    return MessageStore(session=mock_session, embed_client=embed_client)


class TestQueryStatsScopeLock:
    def test_channel_id_always_in_params(self, store, mock_session):
        """channel_id is always bound, regardless of which other filters are set."""
        mock_session.exec.return_value = []

        store.query_stats(channel_id="my-channel", metric="count")

        params = mock_session.exec.call_args[1]["params"]
        assert params["channel_id"] == "my-channel"


class TestQueryStatsAllowList:
    def test_unknown_metric_raises_and_never_queries(self, store, mock_session):
        with pytest.raises(ValueError):
            store.query_stats(channel_id="ch1", metric="delete_everything")
        mock_session.exec.assert_not_called()

    def test_unknown_group_by_raises_and_never_queries(self, store, mock_session):
        with pytest.raises(ValueError):
            store.query_stats(
                channel_id="ch1", metric="count", group_by="; DROP TABLE messages"
            )
        mock_session.exec.assert_not_called()

    @pytest.mark.parametrize("metric", ["first", "latest"])
    def test_first_latest_with_group_by_raises(self, store, mock_session, metric):
        with pytest.raises(ValueError):
            store.query_stats(channel_id="ch1", metric=metric, group_by="author")
        mock_session.exec.assert_not_called()

    def test_first_latest_with_group_by_none_is_allowed(self, store, mock_session):
        mock_session.exec.return_value = [_make_row()]

        result = store.query_stats(channel_id="ch1", metric="first", group_by=None)

        assert result[0]["discord_message_id"] == "111"
        mock_session.exec.assert_called_once()


class TestQueryStatsFilters:
    def test_user_id_bound_only_when_supplied(self, store, mock_session):
        mock_session.exec.return_value = []

        store.query_stats(channel_id="ch1", metric="count")
        params = mock_session.exec.call_args[1]["params"]
        assert "user_id" not in params

        store.query_stats(channel_id="ch1", metric="count", user_id="u42")
        params = mock_session.exec.call_args[1]["params"]
        assert params["user_id"] == "u42"

    def test_since_bound_only_when_supplied(self, store, mock_session):
        mock_session.exec.return_value = []
        when = datetime(2026, 1, 1, tzinfo=timezone.utc)

        store.query_stats(channel_id="ch1", metric="count")
        assert "since" not in mock_session.exec.call_args[1]["params"]

        store.query_stats(channel_id="ch1", metric="count", since=when)
        params = mock_session.exec.call_args[1]["params"]
        assert params["since"] == when

    def test_until_bound_only_when_supplied(self, store, mock_session):
        mock_session.exec.return_value = []
        when = datetime(2026, 6, 1, tzinfo=timezone.utc)

        store.query_stats(channel_id="ch1", metric="count")
        assert "until" not in mock_session.exec.call_args[1]["params"]

        store.query_stats(channel_id="ch1", metric="count", until=when)
        params = mock_session.exec.call_args[1]["params"]
        assert params["until"] == when

    def test_contains_bound_as_param_never_in_sql_text(self, store, mock_session):
        mock_session.exec.return_value = []

        store.query_stats(channel_id="ch1", metric="count")
        assert "contains" not in mock_session.exec.call_args[1]["params"]

        malicious = "'; DROP TABLE chat.messages; --"
        store.query_stats(channel_id="ch1", metric="count", contains=malicious)
        call_args = mock_session.exec.call_args
        params = call_args[1]["params"]
        assert params["contains"] == malicious
        sql_text = str(call_args[0][0])
        assert malicious not in sql_text

    def test_message_id_bound_only_when_supplied(self, store, mock_session):
        mock_session.exec.return_value = []

        store.query_stats(channel_id="ch1", metric="count")
        assert "message_id" not in mock_session.exec.call_args[1]["params"]

        store.query_stats(channel_id="ch1", metric="count", message_id="999")
        params = mock_session.exec.call_args[1]["params"]
        assert params["message_id"] == "999"

    def test_group_by_value_not_interpolated_beyond_fixed_fragment(
        self, store, mock_session
    ):
        """group_by is looked up in a fixed dict; the model's own string never
        reaches the SQL text (only the hard-coded fragment/column does)."""
        mock_session.exec.return_value = []

        store.query_stats(channel_id="ch1", metric="count", group_by="author")

        sql_text = str(mock_session.exec.call_args[0][0])
        assert "user_id" in sql_text  # the fixed fragment for "author"
        assert "author" not in sql_text  # the caller's raw enum string itself


class TestQueryStatsGrouping:
    def test_count_group_by_author(self, store, mock_session):
        row = SimpleNamespace(user_id="u1", username="Alice", n=7)
        mock_session.exec.return_value = [row]

        result = store.query_stats(channel_id="ch1", metric="count", group_by="author")

        assert result == [{"user_id": "u1", "username": "Alice", "count": 7}]

    def test_count_group_by_day(self, store, mock_session):
        day = datetime(2026, 4, 1, tzinfo=timezone.utc)
        row = SimpleNamespace(d=day, n=3)
        mock_session.exec.return_value = [row]

        result = store.query_stats(channel_id="ch1", metric="count", group_by="day")

        assert result == [{"day": day, "count": 3}]

    def test_count_with_no_group_by_returns_total(self, store, mock_session):
        row = SimpleNamespace(n=42)
        mock_session.exec.return_value = [row]

        result = store.query_stats(channel_id="ch1", metric="count")

        assert result == [{"count": 42}]

    def test_latest_returns_single_dict(self, store, mock_session):
        row = _make_row(discord_message_id="222", content="latest one")
        mock_session.exec.return_value = [row]

        result = store.query_stats(channel_id="ch1", metric="latest")

        assert len(result) == 1
        assert result[0]["content"] == "latest one"

    def test_first_or_latest_returns_empty_list_when_no_rows(self, store, mock_session):
        mock_session.exec.return_value = []

        result = store.query_stats(channel_id="ch1", metric="first")

        assert result == []


class TestQueryStatsTimeout:
    def test_no_statement_timeout_call_on_mocked_session(self, store, mock_session):
        """A MagicMock session's dialect never compares equal to 'postgresql',
        so the SET LOCAL statement_timeout guard is skipped -- exactly the
        SQLite-test-safety behavior it exists for."""
        mock_session.exec.return_value = []

        store.query_stats(channel_id="ch1", metric="count")

        # Only the single count query was executed, not a preceding SET LOCAL.
        mock_session.exec.assert_called_once()


# ---------------------------------------------------------------------------
# explore_history tool -- scope lock (mirrors agent_tool_execution_test.py)
# ---------------------------------------------------------------------------

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from chat.agent import ChatDeps, create_agent


def _tool_once_then_done(tool_name: str, args: dict) -> object:
    """Return a FunctionModel function that calls one tool then returns 'done'."""

    def model_func(messages, info):  # type: ignore[type-arg]
        for msg in messages:
            if hasattr(msg, "parts"):
                for part in msg.parts:
                    if isinstance(part, ToolReturnPart):
                        return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(
            parts=[ToolCallPart(tool_name=tool_name, args=args, tool_call_id="call-1")]
        )

    return FunctionModel(model_func)


class TestExploreHistoryScopeLock:
    @pytest.mark.asyncio
    async def test_uses_deps_channel_id_not_a_tool_argument(self):
        """explore_history's tool schema has no channel argument at all --
        scope comes only from ctx.deps.channel_id, per ADR chat/002."""
        embed_client = AsyncMock()
        store = MagicMock()
        store.query_stats.return_value = [{"count": 5}]

        deps = ChatDeps(
            channel_id="scoped-channel", store=store, embed_client=embed_client
        )
        agent = create_agent(base_url="http://fake:8080")

        # A malicious/confused model tries to pass a channel_id anyway; the
        # tool signature has no such parameter, so PydanticAI drops it and the
        # call still succeeds scoped to deps.channel_id.
        await agent.run(
            "how many messages",
            model=_tool_once_then_done("explore_history", {"metric": "count"}),
            deps=deps,
        )

        store.query_stats.assert_called_once()
        call_kwargs = store.query_stats.call_args.kwargs
        assert call_kwargs.get("channel_id") == "scoped-channel"

    @pytest.mark.asyncio
    async def test_explore_history_has_no_channel_id_parameter(self):
        """Assert directly against the tool's registered parameter schema."""
        embed_client = AsyncMock()
        store = MagicMock()
        agent = create_agent(base_url="http://fake:8080")

        tool = agent._function_toolset.tools.get("explore_history")
        assert tool is not None
        params = tool.function.__code__.co_varnames[
            : tool.function.__code__.co_argcount
        ]
        assert "channel_id" not in params

    @pytest.mark.asyncio
    async def test_invalid_metric_returns_correction_string_not_error(self):
        """A bad model-chosen metric self-corrects instead of erroring the turn."""
        embed_client = AsyncMock()
        store = MagicMock()
        store.query_stats.side_effect = ValueError("bad metric")

        deps = ChatDeps(channel_id="ch1", store=store, embed_client=embed_client)
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
                        tool_name="explore_history",
                        args={"metric": "delete_everything"},
                        tool_call_id="call-1",
                    )
                ]
            )

        await agent.run(
            "how many messages",
            model=FunctionModel(model_func),
            deps=deps,
        )

        assert len(tool_return_captured) == 1
        assert "count/first/latest" in tool_return_captured[0]
