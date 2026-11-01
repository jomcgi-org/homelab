"""Tests for search_similar() model_validate deserialization from raw SQL rows.

Raw SQL via pgvector returns row objects (not pre-built Message instances).
search_similar() calls Message.model_validate(row) on each result.  These
tests verify that:
  - Dict-shaped rows are correctly deserialized into Message instances
  - The embedding string representation ("[0.1, 0.2, ...]") from pgvector
    is parsed correctly via the _parse_embedding field_validator
  - Multiple rows each become distinct Message objects
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from chat.models import Message
from chat.store import MessageStore


@pytest.fixture
def mock_session():
    return MagicMock()


@pytest.fixture
def store(mock_session):
    embed_client = AsyncMock()
    embed_client.embed_batch.return_value = [[0.0] * 1024]
    return MessageStore(session=mock_session, embed_client=embed_client)


def _raw_row(**overrides) -> dict:
    """Build a dict representing a raw SQL row for chat.messages."""
    defaults = {
        "id": 1,
        "discord_message_id": "msg-1",
        "channel_id": "ch1",
        "user_id": "u1",
        "username": "Alice",
        "content": "Hello from raw row",
        "is_bot": False,
        "embedding": [0.0] * 1024,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    defaults.update(overrides)
    return defaults


class TestSearchSimilarModelValidate:
    def test_dict_row_is_deserialized_to_message(self, store, mock_session):
        """search_similar deserializes a dict-shaped raw SQL row into a Message."""
        raw = _raw_row(id=42, content="Semantic match", user_id="u99")
        mock_session.exec.return_value = [raw]

        results = store.search_similar(
            channel_id="ch1",
            query_embedding=[0.1] * 1024,
        )

        assert len(results) == 1
        msg = results[0]
        assert isinstance(msg, Message)
        assert msg.id == 42
        assert msg.content == "Semantic match"
        assert msg.user_id == "u99"

    def test_multiple_dict_rows_produce_multiple_messages(self, store, mock_session):
        """search_similar handles multiple rows, each deserialized as a Message."""
        rows = [
            _raw_row(id=1, content="first"),
            _raw_row(id=2, content="second"),
            _raw_row(id=3, content="third"),
        ]
        mock_session.exec.return_value = rows

        results = store.search_similar(
            channel_id="ch1",
            query_embedding=[0.1] * 1024,
        )

        assert len(results) == 3
        assert all(isinstance(r, Message) for r in results)
        contents = [r.content for r in results]
        assert contents == ["first", "second", "third"]

    def test_channel_id_preserved_after_deserialization(self, store, mock_session):
        """channel_id is preserved correctly after model_validate deserialization."""
        raw = _raw_row(channel_id="special-channel-42")
        mock_session.exec.return_value = [raw]

        results = store.search_similar(
            channel_id="special-channel-42",
            query_embedding=[0.0] * 1024,
        )

        assert results[0].channel_id == "special-channel-42"

    def test_embedding_string_representation_is_parsed(self, store, mock_session):
        """Rows where embedding is a pgvector string '[0.1, ...]' are deserialized.

        pgvector stores embeddings as text in raw SQL results.
        Message._parse_embedding handles the string-to-list conversion.
        """
        import json

        embedding_values = [0.1, 0.2, 0.3] + [0.0] * 1021
        raw = _raw_row(embedding=json.dumps(embedding_values))
        mock_session.exec.return_value = [raw]

        results = store.search_similar(
            channel_id="ch1",
            query_embedding=[0.0] * 1024,
        )

        assert len(results) == 1
        msg = results[0]
        assert isinstance(msg, Message)
        assert abs(msg.embedding[0] - 0.1) < 1e-6
        assert abs(msg.embedding[1] - 0.2) < 1e-6

    def test_is_bot_field_preserved(self, store, mock_session):
        """is_bot field is correctly preserved after deserialization."""
        raw = _raw_row(is_bot=True, user_id="bot-99")
        mock_session.exec.return_value = [raw]

        results = store.search_similar(
            channel_id="ch1",
            query_embedding=[0.0] * 1024,
        )

        assert results[0].is_bot is True

    def test_username_preserved_after_deserialization(self, store, mock_session):
        """username is preserved correctly after model_validate."""
        raw = _raw_row(username="SpecialUser")
        mock_session.exec.return_value = [raw]

        results = store.search_similar(
            channel_id="ch1",
            query_embedding=[0.0] * 1024,
        )

        assert results[0].username == "SpecialUser"
