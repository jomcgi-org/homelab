"""Tests for the synchronous Discord output ceiling.

Commit cf57ba20b removed a hardcoded ``max_tokens=16384`` because combining
that reservation with prompts overflowed a small model context window. Meta
Muse Spark has a 1M token context, so these tests pin a configurable 512-token
Discord ceiling without restoring the old context-window failure mode.
"""

from unittest.mock import patch

from pydantic_ai import ModelSettings

import shared.inference
from chat.agent import create_agent, create_fact_check_agent


class TestChatMaxTokens:
    def test_default_is_512(self, monkeypatch):
        """An unset environment variable uses the Discord-sized ceiling."""
        monkeypatch.delenv(shared.inference.CHAT_MAX_TOKENS_ENV, raising=False)

        assert shared.inference.chat_max_tokens() == 512

    def test_reads_environment(self, monkeypatch):
        """A positive configured ceiling is returned as an integer."""
        monkeypatch.setenv(shared.inference.CHAT_MAX_TOKENS_ENV, " 768 ")

        assert shared.inference.chat_max_tokens() == 768

    def test_zero_disables_cap(self, monkeypatch):
        """Zero restores the uncapped provider behavior."""
        monkeypatch.setenv(shared.inference.CHAT_MAX_TOKENS_ENV, "0")

        assert shared.inference.chat_max_tokens() is None

    def test_negative_disables_cap(self, monkeypatch):
        """Negative values also restore the uncapped provider behavior."""
        monkeypatch.setenv(shared.inference.CHAT_MAX_TOKENS_ENV, "-1")

        assert shared.inference.chat_max_tokens() is None

    def test_non_integer_falls_back_to_512(self, monkeypatch):
        """Invalid configuration fails safe to the Discord-sized ceiling."""
        monkeypatch.setenv(shared.inference.CHAT_MAX_TOKENS_ENV, "many")

        assert shared.inference.chat_max_tokens() == 512

    def test_whitespace_is_treated_as_unset(self, monkeypatch):
        """Whitespace-only configuration uses the code default."""
        monkeypatch.setenv(shared.inference.CHAT_MAX_TOKENS_ENV, "  \t ")

        assert shared.inference.chat_max_tokens() == 512

    def test_create_agent_includes_default_max_tokens(self, monkeypatch):
        """The default Discord agent reserves at most 512 output tokens."""
        monkeypatch.delenv(shared.inference.CHAT_MAX_TOKENS_ENV, raising=False)
        with (
            patch("chat.agent.LLAMA_CPP_URL", "http://fake:8080"),
            patch(
                "chat.agent.shared.inference.chat_reasoning_effort",
                return_value="minimal",
            ),
        ):
            agent = create_agent(base_url="http://fake:8080")

        assert agent.model_settings == {
            "temperature": 1.0,
            "top_p": 0.95,
            "presence_penalty": 1.5,
            "openai_reasoning_effort": "minimal",
            "max_tokens": 512,
        }

    def test_fact_check_agent_includes_max_tokens(self, monkeypatch):
        """The synchronous Discord fact-check path uses the same ceiling."""
        monkeypatch.setenv(shared.inference.CHAT_MAX_TOKENS_ENV, "384")
        with patch(
            "chat.agent.shared.inference.chat_reasoning_effort",
            return_value="low",
        ):
            agent = create_fact_check_agent(base_url="http://fake:8080")

        assert agent.model_settings == {
            "temperature": 1.0,
            "top_p": 0.95,
            "presence_penalty": 1.5,
            "openai_reasoning_effort": "low",
            "max_tokens": 384,
        }

    def test_uncapped_mode_has_no_concrete_max_tokens(self, monkeypatch):
        """None leaves the output limit to the provider."""
        monkeypatch.setenv(shared.inference.CHAT_MAX_TOKENS_ENV, "0")
        with patch("chat.agent.LLAMA_CPP_URL", "http://fake:8080"):
            agent = create_agent(base_url="http://fake:8080")

        assert agent.model_settings is not None
        # Key ABSENT, not present-and-None: PydanticAI reads it as
        # model_settings.get("max_tokens", OMIT), so a None value would be
        # forwarded to the provider as an explicit null instead of dropped.
        assert "max_tokens" not in agent.model_settings

    def test_explicit_model_settings_pass_through_untouched(self):
        """Hosted household and WhatsApp settings do not gain the cap."""
        explicit = ModelSettings(temperature=0.7, top_p=0.95)

        with (
            patch("chat.agent.LLAMA_CPP_URL", "http://fake:8080"),
            patch(
                "chat.agent.shared.inference.chat_reasoning_effort"
            ) as reasoning_effort,
            patch("chat.agent.shared.inference.chat_max_tokens") as max_tokens,
        ):
            agent = create_agent(
                base_url="http://fake:8080",
                model_settings=explicit,
                channel="whatsapp",
            )

        assert agent.model_settings is explicit
        assert "openai_reasoning_effort" not in agent.model_settings
        assert "max_tokens" not in agent.model_settings
        reasoning_effort.assert_not_called()
        max_tokens.assert_not_called()

    def test_spark_profile_supports_sampling_parameters(self):
        """Spark retains persona sampling alongside reasoning and token limits.

        PydanticAI strips temperature, top_p and presence_penalty at request
        time when reasoning is on, but only for profiles with
        openai_supports_reasoning, which openai_model_profile sets solely for
        o-series and gpt-5 name prefixes. Spark matches neither, so the sampling
        parameters survive. This test catches model renames or library upgrades
        that would silently change that profile behavior.
        """
        from pydantic_ai.profiles.openai import openai_model_profile

        profile = openai_model_profile(shared.inference.META_SPARK_MODEL)

        assert profile.openai_supports_reasoning is False
