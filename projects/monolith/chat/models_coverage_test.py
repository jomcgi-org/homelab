"""Coverage tests for MessageLock and ChannelSummary models, plus max_length constraints.

Fills gaps identified in the coverage review:
- MessageLock: completed default (False), claimed_at UTC default, column names,
  discord_message_id as primary key.
- ChannelSummary: message_count default (0), updated_at UTC default, columns,
  channel_id unique constraint.
- Blob.sha256 and Attachment.blob_sha256 max_length=64 constraints.
"""

from __future__ import annotations

from datetime import timezone

from chat.models import (
    Attachment,
    Blob,
    ChannelSummary,
    MessageLock,
)


class TestMessageLock:
    def test_table_name(self):
        """MessageLock maps to chat.message_locks table."""
        assert MessageLock.__tablename__ == "message_locks"
        assert MessageLock.__table_args__["schema"] == "chat"

    def test_column_names(self):
        """MessageLock has exactly the expected columns."""
        columns = {c.name for c in MessageLock.__table__.columns}
        assert columns == {
            "discord_message_id",
            "channel_id",
            "claimed_at",
            "completed",
        }

    def test_discord_message_id_is_primary_key(self):
        """discord_message_id is the primary key column."""
        pk_cols = {c.name for c in MessageLock.__table__.primary_key.columns}
        assert pk_cols == {"discord_message_id"}

    def test_completed_default_is_false(self):
        """MessageLock.completed defaults to False."""
        lock = MessageLock(discord_message_id="msg-1", channel_id="ch-1")
        assert lock.completed is False

    def test_completed_can_be_set_true(self):
        """MessageLock.completed can be set to True."""
        lock = MessageLock(
            discord_message_id="msg-2", channel_id="ch-2", completed=True
        )
        assert lock.completed is True

    def test_claimed_at_default_is_utc(self):
        """MessageLock.claimed_at default factory produces a UTC-aware datetime."""
        lock = MessageLock(discord_message_id="msg-3", channel_id="ch-3")
        assert lock.claimed_at is not None
        assert lock.claimed_at.tzinfo is not None
        assert lock.claimed_at.tzinfo == timezone.utc

    def test_claimed_at_can_be_overridden(self):
        """MessageLock.claimed_at accepts an explicit datetime."""
        from datetime import datetime

        dt = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        lock = MessageLock(discord_message_id="msg-4", channel_id="ch-4", claimed_at=dt)
        assert lock.claimed_at == dt

    def test_construction_with_all_fields(self):
        """MessageLock can be constructed with all fields specified."""
        lock = MessageLock(
            discord_message_id="msg-5",
            channel_id="ch-5",
            completed=False,
        )
        assert lock.discord_message_id == "msg-5"
        assert lock.channel_id == "ch-5"


class TestChannelSummary:
    def test_table_name(self):
        """ChannelSummary maps to chat.channel_summaries table."""
        assert ChannelSummary.__tablename__ == "channel_summaries"
        assert ChannelSummary.__table_args__["schema"] == "chat"

    def test_column_names(self):
        """ChannelSummary has exactly the expected columns."""
        columns = {c.name for c in ChannelSummary.__table__.columns}
        assert columns == {
            "id",
            "channel_id",
            "summary",
            "message_count",
            "last_message_id",
            "updated_at",
        }

    def test_channel_id_has_unique_constraint(self):
        """ChannelSummary.channel_id is unique."""
        col = ChannelSummary.__table__.c["channel_id"]
        assert col.unique is True

    def test_message_count_default_is_zero(self):
        """ChannelSummary.message_count defaults to 0."""
        summary = ChannelSummary(
            channel_id="ch-1",
            summary="some summary",
            last_message_id=1,
        )
        assert summary.message_count == 0

    def test_message_count_can_be_set(self):
        """ChannelSummary.message_count can be set to a positive integer."""
        summary = ChannelSummary(
            channel_id="ch-2",
            summary="some summary",
            message_count=42,
            last_message_id=2,
        )
        assert summary.message_count == 42

    def test_updated_at_default_is_utc(self):
        """ChannelSummary.updated_at default factory produces a UTC-aware datetime."""
        summary = ChannelSummary(
            channel_id="ch-3",
            summary="some summary",
            last_message_id=3,
        )
        assert summary.updated_at is not None
        assert summary.updated_at.tzinfo is not None
        assert summary.updated_at.tzinfo == timezone.utc

    def test_updated_at_can_be_overridden(self):
        """ChannelSummary.updated_at accepts an explicit datetime."""
        from datetime import datetime

        dt = datetime(2024, 1, 15, 9, 30, 0, tzinfo=timezone.utc)
        summary = ChannelSummary(
            channel_id="ch-4",
            summary="some summary",
            last_message_id=4,
            updated_at=dt,
        )
        assert summary.updated_at == dt

    def test_id_defaults_none(self):
        """ChannelSummary.id defaults to None (auto-assigned by DB)."""
        summary = ChannelSummary(
            channel_id="ch-5",
            summary="x",
            last_message_id=5,
        )
        assert summary.id is None


class TestMaxLengthConstraints:
    def test_blob_sha256_max_length_is_64(self):
        """Blob.sha256 column has max_length=64 set in Field."""
        col = Blob.__table__.c["sha256"]
        # SQLAlchemy String column carries the length on its type
        assert col.type.length == 64

    def test_attachment_blob_sha256_max_length_is_64(self):
        """Attachment.blob_sha256 column has max_length=64 set in Field."""
        col = Attachment.__table__.c["blob_sha256"]
        assert col.type.length == 64
