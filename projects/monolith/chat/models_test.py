"""Tests for chat SQLModel definitions."""

import pytest
from pydantic import ValidationError
from sqlmodel import SQLModel

from chat.models import (
    Attachment,
    Blob,
    ChannelSummary,
    Message,
    MessageLock,
    UserChannelSummary,
)


def _get_table_args_dict(model_class: type) -> dict:
    """Return the kwargs dict from __table_args__, handling both dict and tuple forms.

    SQLModel table classes declare __table_args__ as either a plain dict
    (simple case) or a tuple ending with a dict (when constraints are also
    present, e.g. UserChannelSummary).  This helper normalises both forms so
    tests can inspect keyword options without branching per model.
    """
    args = model_class.__table_args__
    if isinstance(args, dict):
        return args
    # Tuple form: the last dict element holds the keyword arguments.
    for item in reversed(args):
        if isinstance(item, dict):
            return item
    return {}


class TestMessageModel:
    def test_message_table_name(self):
        """Message model maps to chat.messages table."""
        assert Message.__tablename__ == "messages"
        assert Message.__table_args__["schema"] == "chat"

    def test_message_has_required_fields(self):
        """Message model has all expected columns."""
        columns = {c.name for c in Message.__table__.columns}
        expected = {
            "id",
            "discord_message_id",
            "channel_id",
            "user_id",
            "username",
            "content",
            "is_bot",
            "embedding",
            "created_at",
            "thinking",
        }
        assert expected == columns

    def test_message_is_bot_defaults_false(self):
        """is_bot field defaults to False."""
        msg = Message(
            discord_message_id="123",
            channel_id="456",
            user_id="789",
            username="test",
            content="hello",
            embedding=[0.0] * 1024,
        )
        assert msg.is_bot is False

    def test_embedding_validator_parses_string(self):
        """Embedding validator converts pgvector string to list."""
        msg = Message.model_validate(
            {
                "discord_message_id": "1",
                "channel_id": "c",
                "user_id": "u",
                "username": "bot",
                "content": "hi",
                "embedding": "[0.1,0.2,0.3]",
            }
        )
        assert msg.embedding == [0.1, 0.2, 0.3]

    def test_embedding_validator_passes_list_through(self):
        """Embedding validator leaves a native list unchanged."""
        vec = [0.4, 0.5, 0.6]
        msg = Message.model_validate(
            {
                "discord_message_id": "2",
                "channel_id": "c",
                "user_id": "u",
                "username": "bot",
                "content": "hi",
                "embedding": vec,
            }
        )
        assert msg.embedding == vec

    def test_embedding_validator_raises_on_invalid_json_string(self):
        """Embedding validator raises ValidationError for non-JSON strings."""
        with pytest.raises(ValidationError):
            Message.model_validate(
                {
                    "discord_message_id": "3",
                    "channel_id": "c",
                    "user_id": "u",
                    "username": "bot",
                    "content": "hi",
                    "embedding": "not-json",
                }
            )

    def test_embedding_validator_raises_on_empty_string(self):
        """Embedding validator raises ValidationError for empty string."""
        with pytest.raises(ValidationError):
            Message.model_validate(
                {
                    "discord_message_id": "4",
                    "channel_id": "c",
                    "user_id": "u",
                    "username": "bot",
                    "content": "hi",
                    "embedding": "",
                }
            )

    @pytest.mark.parametrize("bad_value", [None, 42, {"key": "value"}])
    def test_embedding_validator_does_not_intercept_non_string_types(self, bad_value):
        """Validator skips non-string inputs; Pydantic then rejects non-list[float] values."""
        with pytest.raises(ValidationError):
            Message.model_validate(
                {
                    "discord_message_id": "5",
                    "channel_id": "c",
                    "user_id": "u",
                    "username": "bot",
                    "content": "hi",
                    "embedding": bad_value,
                }
            )


class TestAttachmentModel:
    def test_attachment_table_name(self):
        """Attachment model maps to chat.attachments table."""
        assert Attachment.__tablename__ == "attachments"
        assert Attachment.__table_args__["schema"] == "chat"

    def test_attachment_has_required_fields(self):
        """Attachment model has all expected columns."""
        columns = {c.name for c in Attachment.__table__.columns}
        expected = {
            "id",
            "message_id",
            "blob_sha256",
            "filename",
        }
        assert expected == columns

    def test_attachment_construction(self):
        """Attachment can be constructed with all fields."""
        att = Attachment(
            message_id=1,
            blob_sha256="abc123",
            filename="photo.png",
        )
        assert att.blob_sha256 == "abc123"
        assert att.filename == "photo.png"


class TestBlobModel:
    def test_blob_table_name(self):
        """Blob model maps to chat.blobs table."""
        assert Blob.__tablename__ == "blobs"
        assert Blob.__table_args__["schema"] == "chat"

    def test_blob_has_required_fields(self):
        """Blob model has all expected columns."""
        columns = {c.name for c in Blob.__table__.columns}
        expected = {
            "sha256",
            "content_type",
            "description",
        }
        assert expected == columns

    def test_blob_construction(self):
        """Blob can be constructed with all metadata fields."""
        blob = Blob(
            sha256="deadbeef" * 8,
            content_type="image/png",
            description="A photo of a cat",
        )
        assert blob.sha256 == "deadbeef" * 8
        assert blob.content_type == "image/png"
        assert blob.description == "A photo of a cat"

    def test_blob_description_defaults_empty(self):
        """Blob.description defaults to empty string."""
        blob = Blob(
            sha256="abcd1234" * 8,
            content_type="image/gif",
        )
        assert blob.description == ""


# ---------------------------------------------------------------------------
# extend_existing — prevent MetaData conflicts on repeated imports
# ---------------------------------------------------------------------------


class TestExtendExisting:
    """All chat SQLModel tables must declare extend_existing=True.

    SQLAlchemy raises an error when a table is registered with the same
    MetaData more than once (e.g. during test collection or module reload)
    unless extend_existing=True is set in __table_args__.  These tests pin
    that property so it cannot be silently removed.
    """

    @pytest.mark.parametrize(
        "model_class",
        [Message, Blob, Attachment, UserChannelSummary, MessageLock, ChannelSummary],
        ids=[
            "Message",
            "Blob",
            "Attachment",
            "UserChannelSummary",
            "MessageLock",
            "ChannelSummary",
        ],
    )
    def test_extend_existing_is_true(self, model_class):
        """__table_args__ must contain extend_existing=True for every chat model."""
        kwargs = _get_table_args_dict(model_class)
        assert kwargs.get("extend_existing") is True, (
            f"{model_class.__name__}.__table_args__ is missing 'extend_existing': True"
        )

    @pytest.mark.parametrize(
        "model_class",
        [Message, Blob, Attachment, UserChannelSummary, MessageLock, ChannelSummary],
        ids=[
            "Message",
            "Blob",
            "Attachment",
            "UserChannelSummary",
            "MessageLock",
            "ChannelSummary",
        ],
    )
    def test_schema_is_chat(self, model_class):
        """__table_args__ must still declare schema='chat' for every chat model."""
        kwargs = _get_table_args_dict(model_class)
        assert kwargs.get("schema") == "chat", (
            f"{model_class.__name__}.__table_args__ is missing 'schema': 'chat'"
        )

    def test_user_channel_summary_table_args_is_tuple(self):
        """UserChannelSummary uses a tuple form of __table_args__ (holds a constraint)."""
        assert isinstance(UserChannelSummary.__table_args__, tuple)

    def test_user_channel_summary_extend_existing_in_kwargs_dict(self):
        """extend_existing is in the kwargs dict at the end of the UserChannelSummary tuple."""
        kwargs = _get_table_args_dict(UserChannelSummary)
        assert kwargs.get("extend_existing") is True


# ---------------------------------------------------------------------------
# Field default values — MessageLock, ChannelSummary, UserChannelSummary
# ---------------------------------------------------------------------------


class TestMessageLockDefaults:
    """MessageLock field defaults."""

    def test_completed_defaults_false(self):
        """MessageLock.completed defaults to False."""
        lock = MessageLock(
            discord_message_id="msg-1",
            channel_id="ch-1",
        )
        assert lock.completed is False

    def test_claimed_at_defaults_to_current_utc(self):
        """MessageLock.claimed_at auto-populates with a UTC datetime."""
        lock = MessageLock(
            discord_message_id="msg-2",
            channel_id="ch-2",
        )
        assert lock.claimed_at is not None
        assert lock.claimed_at.tzinfo is not None

    def test_id_defaults_none(self):
        """MessageLock has no surrogate pk field; discord_message_id is the pk."""
        # Ensure no 'id' attribute leaks onto MessageLock.
        columns = {c.name for c in MessageLock.__table__.columns}
        assert "discord_message_id" in columns
        assert "id" not in columns


class TestChannelSummaryDefaults:
    """ChannelSummary field defaults."""

    def test_message_count_defaults_zero(self):
        """ChannelSummary.message_count defaults to 0."""
        cs = ChannelSummary(
            channel_id="ch-1",
            summary="A summary.",
            last_message_id=42,
        )
        assert cs.message_count == 0

    def test_updated_at_defaults_to_current_utc(self):
        """ChannelSummary.updated_at auto-populates with a UTC datetime."""
        cs = ChannelSummary(
            channel_id="ch-2",
            summary="Another summary.",
            last_message_id=99,
        )
        assert cs.updated_at is not None
        assert cs.updated_at.tzinfo is not None

    def test_id_defaults_none(self):
        """ChannelSummary.id is None until persisted."""
        cs = ChannelSummary(
            channel_id="ch-3",
            summary="s",
            last_message_id=1,
        )
        assert cs.id is None


class TestUserChannelSummaryDefaults:
    """UserChannelSummary field defaults."""

    def test_updated_at_defaults_to_current_utc(self):
        """UserChannelSummary.updated_at auto-populates with a UTC datetime."""
        ucs = UserChannelSummary(
            channel_id="ch-1",
            user_id="u-1",
            username="alice",
            summary="A user summary.",
            last_message_id=7,
        )
        assert ucs.updated_at is not None
        assert ucs.updated_at.tzinfo is not None

    def test_id_defaults_none(self):
        """UserChannelSummary.id is None until persisted."""
        ucs = UserChannelSummary(
            channel_id="ch-2",
            user_id="u-2",
            username="bob",
            summary="s",
            last_message_id=1,
        )
        assert ucs.id is None

    def test_has_required_columns(self):
        """UserChannelSummary has all expected columns."""
        columns = {c.name for c in UserChannelSummary.__table__.columns}
        expected = {
            "id",
            "channel_id",
            "user_id",
            "username",
            "summary",
            "last_message_id",
            "updated_at",
        }
        assert expected == columns
