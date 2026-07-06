"""Additional coverage for MessageStore -- search_similar() with mocked Session."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock

import pytest

from chat.models import Message
from chat.store import MessageStore


def _make_message(
    id: int,
    channel_id: str = "ch1",
    user_id: str = "u1",
    content: str = "hello",
) -> Message:
    return Message(
        id=id,
        discord_message_id=str(id),
        channel_id=channel_id,
        user_id=user_id,
        username="Alice",
        content=content,
        is_bot=False,
        embedding=[0.0] * 1024,
        created_at=datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def mock_session():
    return MagicMock()


@pytest.fixture
def store(mock_session):
    embed_client = AsyncMock()
    embed_client.embed_batch.return_value = [[0.0] * 1024]
    return MessageStore(session=mock_session, embed_client=embed_client)


class TestSearchSimilar:
    def test_returns_messages_from_exec(self, store, mock_session):
        """search_similar returns Message objects produced by session.exec."""
        msg = _make_message(id=1)
        mock_session.exec.return_value = [msg]

        results = store.search_similar(
            channel_id="ch1",
            query_embedding=[0.1] * 1024,
        )

        assert results == [msg]
        mock_session.exec.assert_called_once()

    def test_returns_empty_list_when_no_results(self, store, mock_session):
        """search_similar returns an empty list when exec yields nothing."""
        mock_session.exec.return_value = []

        results = store.search_similar(
            channel_id="ch1",
            query_embedding=[0.0] * 1024,
        )

        assert results == []

    def test_exclude_ids_are_passed_as_params(self, store, mock_session):
        """When exclude_ids is provided the SQL params include excl_0, excl_1, …."""
        mock_session.exec.return_value = []

        store.search_similar(
            channel_id="ch1",
            query_embedding=[0.0] * 1024,
            exclude_ids=[10, 20],
        )

        call_kwargs = mock_session.exec.call_args
        params = call_kwargs[1]["params"]
        assert params.get("excl_0") == 10
        assert params.get("excl_1") == 20

    def test_user_id_filter_included_in_params(self, store, mock_session):
        """When user_id is provided it is included in the SQL params."""
        mock_session.exec.return_value = []

        store.search_similar(
            channel_id="ch1",
            query_embedding=[0.0] * 1024,
            user_id="u42",
        )

        call_kwargs = mock_session.exec.call_args
        params = call_kwargs[1]["params"]
        assert params.get("user_id") == "u42"

    def test_no_user_id_filter_when_not_provided(self, store, mock_session):
        """When user_id is not provided there is no user_id key in the params."""
        mock_session.exec.return_value = []

        store.search_similar(
            channel_id="ch1",
            query_embedding=[0.0] * 1024,
        )

        call_kwargs = mock_session.exec.call_args
        params = call_kwargs[1]["params"]
        assert "user_id" not in params

    def test_channel_id_always_in_params(self, store, mock_session):
        """channel_id is always included in the SQL params."""
        mock_session.exec.return_value = []

        store.search_similar(
            channel_id="my-channel",
            query_embedding=[0.0] * 1024,
        )

        call_kwargs = mock_session.exec.call_args
        params = call_kwargs[1]["params"]
        assert params["channel_id"] == "my-channel"

    def test_limit_param_is_passed(self, store, mock_session):
        """The limit parameter is forwarded to the SQL query."""
        mock_session.exec.return_value = []

        store.search_similar(
            channel_id="ch1",
            query_embedding=[0.0] * 1024,
            limit=3,
        )

        call_kwargs = mock_session.exec.call_args
        params = call_kwargs[1]["params"]
        assert params["limit"] == 3

    def test_exclude_ids_empty_list_treated_as_no_exclusions(self, store, mock_session):
        """Passing exclude_ids=[] produces no excl_ params (no NOT IN clause)."""
        mock_session.exec.return_value = []

        store.search_similar(
            channel_id="ch1",
            query_embedding=[0.0] * 1024,
            exclude_ids=[],
        )

        call_kwargs = mock_session.exec.call_args
        params = call_kwargs[1]["params"]
        excl_keys = [k for k in params if k.startswith("excl_")]
        assert excl_keys == []


class TestLexicalSearch:
    def test_empty_query_returns_empty_without_querying(self, store, mock_session):
        """A blank/whitespace query short-circuits to [] and never hits the DB."""
        assert store.lexical_search(channel_id="ch1", query_text="  ") == []
        mock_session.exec.assert_not_called()

    def test_returns_messages_from_exec(self, store, mock_session):
        msg = _make_message(id=1, content="deploy failed with ORA-00942")
        mock_session.exec.return_value = [msg]

        results = store.lexical_search(channel_id="ch1", query_text="ORA-00942")

        assert results == [msg]
        mock_session.exec.assert_called_once()

    def test_query_text_bound_as_param(self, store, mock_session):
        mock_session.exec.return_value = []

        store.lexical_search(channel_id="ch1", query_text="bandwidth war")

        params = mock_session.exec.call_args[1]["params"]
        assert params["q"] == "bandwidth war"
        assert params["channel_id"] == "ch1"


class TestSearchHybrid:
    def test_rrf_ranks_shared_hit_first(self, store, mock_session):
        """A message returned by BOTH retrievers outranks messages in one list."""
        m1, m2, m3, m4 = (_make_message(id=i) for i in (1, 2, 3, 4))
        # vector order: m1, m2, m3 ; lexical order: m3, m4 -> m3 is in both.
        mock_session.exec.side_effect = [[m1, m2, m3], [m3, m4]]

        results = store.search_hybrid(
            channel_id="ch1",
            query_text="something",
            query_embedding=[0.1] * 1024,
            limit=3,
        )

        assert results[0].id == 3  # shared hit wins on fused score
        assert results[1].id == 1  # top of the vector list next
        assert len(results) == 3

    def test_falls_back_to_vector_when_lexical_empty(self, store, mock_session):
        """When lexical returns nothing, hybrid still yields the vector hits."""
        m1, m2 = _make_message(id=1), _make_message(id=2)
        mock_session.exec.side_effect = [[m1, m2], []]

        results = store.search_hybrid(
            channel_id="ch1",
            query_text="x",
            query_embedding=[0.0] * 1024,
            limit=5,
        )

        assert {m.id for m in results} == {1, 2}

    def test_deduplicates_across_lists(self, store, mock_session):
        """A message in both lists appears once, not twice, in the fused output."""
        m1 = _make_message(id=1)
        mock_session.exec.side_effect = [[m1], [m1]]

        results = store.search_hybrid(
            channel_id="ch1",
            query_text="x",
            query_embedding=[0.0] * 1024,
        )

        assert [m.id for m in results] == [1]

    def test_blank_query_still_returns_vector_hits(self, store, mock_session):
        """A stop-word-only query yields no lexical hits but hybrid still works
        off the vector list (lexical_search short-circuits, so exec is called
        only once, for the vector query)."""
        m1 = _make_message(id=1)
        mock_session.exec.side_effect = [[m1]]

        results = store.search_hybrid(
            channel_id="ch1",
            query_text="   ",
            query_embedding=[0.0] * 1024,
        )

        assert [m.id for m in results] == [1]
