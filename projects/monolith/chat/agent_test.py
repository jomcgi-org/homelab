"""Tests for PydanticAI chat agent."""

from datetime import datetime, timezone
from unittest.mock import patch

import shared.inference
from chat.agent import (
    build_system_prompt,
    create_agent,
    create_fact_check_agent,
    format_context_messages,
)
from chat.models import Attachment, Blob, Message


class TestChatProviderRouting:
    def test_create_agent_uses_hosted_provider_from_environment(self, monkeypatch):
        monkeypatch.setenv(shared.inference.CHAT_MODEL_ENV, "hosted/model")
        monkeypatch.setenv(
            shared.inference.CHAT_BASE_URL_ENV, "https://hosted.example/v1"
        )
        monkeypatch.setenv("OPENROUTER_API_KEY", "hosted-key")

        with (
            patch("chat.agent.Agent"),
            patch("chat.agent.OpenAIChatModel") as model,
            patch("chat.agent.OpenAIProvider") as provider,
        ):
            create_agent()

        provider.assert_called_once_with(
            base_url="https://hosted.example/v1", api_key="hosted-key"
        )
        model.assert_called_once_with("hosted/model", provider=provider.return_value)

    def test_create_agent_falls_back_to_spark(self, monkeypatch):
        monkeypatch.delenv(shared.inference.CHAT_MODEL_ENV, raising=False)
        monkeypatch.delenv(shared.inference.CHAT_BASE_URL_ENV, raising=False)

        with (
            patch("chat.agent.Agent"),
            patch("chat.agent.LLAMA_CPP_URL", "http://spark:8080"),
            patch("chat.agent.OpenAIChatModel") as model,
            patch("chat.agent.OpenAIProvider") as provider,
        ):
            create_agent()

        provider.assert_called_once_with(
            base_url="http://spark:8080/v1", api_key="not-needed"
        )
        model.assert_called_once_with(
            shared.inference.META_SPARK_MODEL,
            provider=provider.return_value,
        )

    def test_explicit_provider_base_url_wins_over_environment(self, monkeypatch):
        monkeypatch.setenv(shared.inference.CHAT_MODEL_ENV, "hosted/model")
        monkeypatch.setenv(
            shared.inference.CHAT_BASE_URL_ENV, "https://hosted.example/v1"
        )

        with (
            patch("chat.agent.Agent"),
            patch("chat.agent.OpenAIChatModel") as model,
            patch("chat.agent.OpenAIProvider") as provider,
        ):
            create_agent(
                provider_base_url="https://explicit.example/v1",
                model_name="explicit/model",
                api_key="explicit-key",
            )

        provider.assert_called_once_with(
            base_url="https://explicit.example/v1", api_key="explicit-key"
        )
        model.assert_called_once_with("explicit/model", provider=provider.return_value)

    def test_explicit_model_and_api_key_win_over_environment(self, monkeypatch):
        monkeypatch.setenv(shared.inference.CHAT_MODEL_ENV, "hosted/model")
        monkeypatch.setenv(
            shared.inference.CHAT_BASE_URL_ENV, "https://hosted.example/v1"
        )

        with (
            patch("chat.agent.Agent"),
            patch("chat.agent.OpenAIChatModel") as model,
            patch("chat.agent.OpenAIProvider") as provider,
        ):
            create_agent(
                base_url="http://explicit-spark:8080",
                model_name="explicit/model",
                api_key="explicit-key",
            )

        provider.assert_called_once_with(
            base_url="http://explicit-spark:8080/v1", api_key="explicit-key"
        )
        model.assert_called_once_with("explicit/model", provider=provider.return_value)

    def test_fact_check_agent_uses_hosted_provider(self, monkeypatch):
        monkeypatch.setenv(shared.inference.CHAT_MODEL_ENV, "hosted/model")
        monkeypatch.setenv(
            shared.inference.CHAT_BASE_URL_ENV, "https://hosted.example/v1"
        )
        monkeypatch.setenv("OPENROUTER_API_KEY", "hosted-key")

        with (
            patch("chat.agent.Agent"),
            patch("chat.agent.OpenAIChatModel") as model,
            patch("chat.agent.OpenAIProvider") as provider,
        ):
            create_fact_check_agent()

        provider.assert_called_once_with(
            base_url="https://hosted.example/v1", api_key="hosted-key"
        )
        model.assert_called_once_with("hosted/model", provider=provider.return_value)

    def test_fact_check_agent_falls_back_to_spark(self, monkeypatch):
        monkeypatch.delenv(shared.inference.CHAT_MODEL_ENV, raising=False)
        monkeypatch.delenv(shared.inference.CHAT_BASE_URL_ENV, raising=False)

        with (
            patch("chat.agent.Agent"),
            patch("chat.agent.LLAMA_CPP_URL", "http://spark:8080"),
            patch("chat.agent.OpenAIChatModel") as model,
            patch("chat.agent.OpenAIProvider") as provider,
        ):
            create_fact_check_agent()

        provider.assert_called_once_with(
            base_url="http://spark:8080/v1", api_key="not-needed"
        )
        model.assert_called_once_with(
            shared.inference.META_SPARK_MODEL,
            provider=provider.return_value,
        )


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


class TestProviderPin:
    """The OpenRouter route must be pinned or the measured throughput is a guess."""

    def _settings(self, monkeypatch, **env):
        for key in (
            shared.inference.CHAT_MODEL_ENV,
            shared.inference.CHAT_BASE_URL_ENV,
            shared.inference.CHAT_PROVIDER_ENV,
        ):
            monkeypatch.delenv(key, raising=False)
        # The OpenAI client rejects an empty api_key outright, so the hosted
        # lane needs one present before it can be constructed at all.
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        with patch("chat.agent.LLAMA_CPP_URL", "http://fake:8080"):
            return create_agent(base_url="http://fake:8080").model_settings

    def test_hosted_lane_carries_the_pin(self, monkeypatch):
        settings = self._settings(
            monkeypatch,
            CHAT_MODEL="openai/gpt-oss-20b",
            CHAT_BASE_URL="https://openrouter.ai/api/v1",
            CHAT_PROVIDER="groq",
        )

        assert settings["extra_body"] == {
            "provider": {"order": ["groq"], "allow_fallbacks": False}
        }

    def test_hosted_lane_without_a_pin_omits_extra_body(self, monkeypatch):
        settings = self._settings(
            monkeypatch,
            CHAT_MODEL="openai/gpt-oss-20b",
            CHAT_BASE_URL="https://openrouter.ai/api/v1",
            CHAT_PROVIDER="   ",
        )

        assert "extra_body" not in settings

    def test_spark_lane_never_carries_a_routing_directive(self, monkeypatch):
        """A pin set while on Spark must not be forwarded; Meta cannot parse it."""
        settings = self._settings(monkeypatch, CHAT_PROVIDER="groq")

        assert "extra_body" not in settings
