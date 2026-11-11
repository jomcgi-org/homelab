"""Tests for UserChannelSummary model."""

from chat.models import UserChannelSummary


class TestUserChannelSummary:
    def test_creates_summary_instance(self):
        """UserChannelSummary can be instantiated with required fields."""
        summary = UserChannelSummary(
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            summary="Alice discussed deployment issues.",
            last_message_id=42,
        )
        assert summary.channel_id == "ch1"
        assert summary.username == "Alice"
        assert summary.summary == "Alice discussed deployment issues."
        assert summary.last_message_id == 42

    def test_default_updated_at_is_utc(self):
        """updated_at defaults to a UTC timestamp."""
        summary = UserChannelSummary(
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            summary="test",
            last_message_id=1,
        )
        assert summary.updated_at is not None
        assert summary.updated_at.tzinfo is not None
