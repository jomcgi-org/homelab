"""Tests for chat model validation edge cases not covered elsewhere.

Covers gaps identified in:
- UserChannelSummary model structure (table name, schema, columns, unique constraint)
- Message._parse_embedding validator with valid JSON that is not a list[float]
  (JSON number, JSON object, JSON array of strings)
- Message.created_at default factory produces a timezone-aware UTC timestamp
"""

from datetime import timezone

import pytest
from pydantic import ValidationError

from chat.models import Message, UserChannelSummary


# ---------------------------------------------------------------------------
# UserChannelSummary — model structure
# ---------------------------------------------------------------------------


class TestUserChannelSummaryModelStructure:
    def test_table_name(self):
        """UserChannelSummary maps to the user_channel_summaries table."""
        assert UserChannelSummary.__tablename__ == "user_channel_summaries"

    def test_schema(self):
        """UserChannelSummary uses the 'chat' schema."""
        # __table_args__ is a tuple when extra kwargs are present; the last element is a dict
        table_args = UserChannelSummary.__table_args__
        schema_dict = table_args[-1] if isinstance(table_args, tuple) else table_args
        assert schema_dict.get("schema") == "chat"

    def test_unique_constraint_on_channel_id_and_user_id(self):
        """UserChannelSummary declares a UniqueConstraint on (channel_id, user_id)."""
        from sqlalchemy import UniqueConstraint

        constraints = UserChannelSummary.__table__.constraints
        unique_constraints = [c for c in constraints if isinstance(c, UniqueConstraint)]
        assert len(unique_constraints) == 1
        constrained_cols = {col.name for col in unique_constraints[0].columns}
        assert constrained_cols == {"channel_id", "user_id"}

    def test_id_is_primary_key(self):
        """UserChannelSummary.id is the primary key column."""
        pk_cols = [c for c in UserChannelSummary.__table__.columns if c.primary_key]
        assert len(pk_cols) == 1
        assert pk_cols[0].name == "id"

    def test_id_defaults_to_none_before_persistence(self):
        """UserChannelSummary.id is None before the row is committed."""
        summary = UserChannelSummary(
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            summary="test",
            last_message_id=1,
        )
        assert summary.id is None


# ---------------------------------------------------------------------------
# Message.created_at — default factory
# ---------------------------------------------------------------------------


class TestMessageCreatedAtDefault:
    def test_created_at_is_set_automatically(self):
        """Message.created_at is populated by the default_factory when not supplied."""
        msg = Message(
            discord_message_id="ts1",
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            content="hi",
            embedding=[0.0] * 1024,
        )
        assert msg.created_at is not None

    def test_created_at_is_timezone_aware(self):
        """Message.created_at carries timezone information (UTC)."""
        msg = Message(
            discord_message_id="ts2",
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            content="hi",
            embedding=[0.0] * 1024,
        )
        assert msg.created_at.tzinfo is not None

    def test_created_at_is_utc(self):
        """Message.created_at timezone is UTC."""
        msg = Message(
            discord_message_id="ts3",
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            content="hi",
            embedding=[0.0] * 1024,
        )
        assert msg.created_at.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# Message._parse_embedding — valid JSON that is not list[float]
# ---------------------------------------------------------------------------


class TestEmbeddingValidatorJsonNonList:
    def test_json_number_string_raises_validation_error(self):
        """_parse_embedding with a JSON number string (e.g. '42') raises ValidationError.

        json.loads('42') → 42 (int), which Pydantic rejects as list[float].
        """
        with pytest.raises(ValidationError):
            Message.model_validate(
                {
                    "discord_message_id": "v1",
                    "channel_id": "c",
                    "user_id": "u",
                    "username": "bot",
                    "content": "hi",
                    "embedding": "42",
                }
            )

    def test_json_object_string_raises_validation_error(self):
        """_parse_embedding with a JSON object string raises ValidationError.

        json.loads('{}') → {} (dict), which Pydantic rejects as list[float].
        """
        with pytest.raises(ValidationError):
            Message.model_validate(
                {
                    "discord_message_id": "v2",
                    "channel_id": "c",
                    "user_id": "u",
                    "username": "bot",
                    "content": "hi",
                    "embedding": "{}",
                }
            )

    def test_json_array_of_strings_raises_validation_error(self):
        """_parse_embedding with a JSON array of non-numeric strings raises ValidationError.

        json.loads('["a","b"]') → ['a','b'] (list[str]); 'a' cannot be coerced to float.
        """
        with pytest.raises(ValidationError):
            Message.model_validate(
                {
                    "discord_message_id": "v3",
                    "channel_id": "c",
                    "user_id": "u",
                    "username": "bot",
                    "content": "hi",
                    "embedding": '["a","b"]',
                }
            )

    def test_json_null_string_raises_validation_error(self):
        """_parse_embedding with 'null' string raises ValidationError.

        json.loads('null') → None, which Pydantic rejects as list[float].
        """
        with pytest.raises(ValidationError):
            Message.model_validate(
                {
                    "discord_message_id": "v4",
                    "channel_id": "c",
                    "user_id": "u",
                    "username": "bot",
                    "content": "hi",
                    "embedding": "null",
                }
            )

    def test_json_numeric_string_array_coerced_to_floats(self):
        """_parse_embedding with a JSON array of numeric strings succeeds via Pydantic coercion.

        json.loads('["1.0","2.0"]') → ['1.0','2.0']; Pydantic coerces each to float.
        """
        msg = Message.model_validate(
            {
                "discord_message_id": "v5",
                "channel_id": "c",
                "user_id": "u",
                "username": "bot",
                "content": "hi",
                "embedding": '["1.0","2.0"]',
            }
        )
        assert msg.embedding == [1.0, 2.0]
