"""Tests for PydanticAI chat agent."""

from datetime import datetime, timezone

from chat.agent import build_system_prompt, format_context_messages
from chat.models import Attachment, Blob, Message


class TestBuildSystemPrompt:
    def test_includes_bot_identity(self):
        """System prompt identifies the bot."""
        prompt = build_system_prompt()
        assert "Discord" in prompt or "chat" in prompt.lower()

    def test_includes_search_first_guidance(self):
        """System prompt includes search-first guidance."""
        prompt = build_system_prompt()
        assert "Search before you respond" in prompt

    def test_states_own_name(self):
        """System prompt tells the model its name is Bosun."""
        prompt = build_system_prompt()
        assert "Bosun" in prompt

    def test_explains_self_mention_recognition(self):
        """System prompt explains that its own <@id> mention means itself."""
        prompt = build_system_prompt()
        assert "mention" in prompt.lower()
        assert "they mean YOU" in prompt

    def test_warns_against_third_party_language_model_advice(self):
        """System prompt tells the model not to disown being a language model."""
        prompt = build_system_prompt()
        assert "language models" in prompt


class TestFormatContextMessages:
    def test_formats_user_message(self):
        """User messages include username and content."""
        msg = Message(
            id=1,
            discord_message_id="1",
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            content="Hello there",
            is_bot=False,
            embedding=[0.0] * 1024,
            created_at=datetime(2026, 4, 3, 12, 0, tzinfo=timezone.utc),
        )
        formatted = format_context_messages([msg])
        assert "Alice" in formatted
        assert "Hello there" in formatted

    def test_formats_bot_message(self):
        """Bot messages are labeled as assistant."""
        msg = Message(
            id=2,
            discord_message_id="2",
            channel_id="ch1",
            user_id="bot",
            username="Bot",
            content="Hi!",
            is_bot=True,
            embedding=[0.0] * 1024,
            created_at=datetime(2026, 4, 3, 12, 1, tzinfo=timezone.utc),
        )
        formatted = format_context_messages([msg])
        assert "Hi!" in formatted

    def test_format_with_image_descriptions(self):
        """format_context_messages includes image descriptions when attachments present."""
        msg = Message(
            id=1,
            discord_message_id="1",
            channel_id="ch1",
            user_id="u1",
            username="Alice",
            content="Check this out",
            is_bot=False,
            embedding=[0.0] * 1024,
            created_at=datetime(2026, 4, 4, 12, 0, tzinfo=timezone.utc),
        )
        attachments_map = {
            1: [
                (
                    Attachment(
                        id=1,
                        message_id=1,
                        blob_sha256="abc123",
                        filename="cat.png",
                    ),
                    Blob(
                        sha256="abc123",
                        content_type="image/png",
                        description="A cat on a keyboard",
                    ),
                ),
            ]
        }
        result = format_context_messages([msg], attachments_map)
        assert "Alice: Check this out" in result
        assert "[Image: A cat on a keyboard]" in result

    def test_format_without_attachments(self):
        """format_context_messages works with empty attachments map."""
        msg = Message(
            id=2,
            discord_message_id="2",
            channel_id="ch1",
            user_id="u1",
            username="Bob",
            content="Just text",
            is_bot=False,
            embedding=[0.0] * 1024,
            created_at=datetime(2026, 4, 4, 12, 0, tzinfo=timezone.utc),
        )
        result = format_context_messages([msg])
        assert "Bob: Just text" in result
        assert "[Image:" not in result


class TestToolGuidancePrompt:
    def test_static_prompt_no_longer_lists_tools(self):
        """build_system_prompt() no longer contains the hand-written tool list."""
        prompt = build_system_prompt()
        assert "You have these tools:" not in prompt

    def test_static_prompt_has_dont_pretend_rule(self):
        """build_system_prompt() includes the don't-pretend-you-searched rule."""
        prompt = build_system_prompt()
        assert "Pretend you looked something up" in prompt
