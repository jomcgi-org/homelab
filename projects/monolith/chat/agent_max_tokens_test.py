"""Test that the chat agent does not set an explicit max_tokens."""

from unittest.mock import patch

from pydantic_ai import ModelSettings

from chat.agent import create_agent, create_fact_check_agent


class TestAgentMaxTokens:
    def test_agent_does_not_set_max_tokens(self):
        """create_agent() should not hardcode max_tokens so vLLM uses remaining context."""
        with patch("chat.agent.LLAMA_CPP_URL", "http://fake:8080"):
            agent = create_agent(base_url="http://fake:8080")
        settings = agent.model_settings
        assert settings is not None
        assert settings.get("max_tokens") is None

    def test_default_settings_include_reasoning_and_sampling_parameters(self):
        """Discord defaults retain persona sampling and use minimal reasoning."""
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
        }

    def test_explicit_model_settings_pass_through_untouched(self):
        """Hosted household and WhatsApp settings do not gain reasoning effort."""
        explicit = ModelSettings(temperature=0.7, top_p=0.95)

        with (
            patch("chat.agent.LLAMA_CPP_URL", "http://fake:8080"),
            patch(
                "chat.agent.shared.inference.chat_reasoning_effort"
            ) as reasoning_effort,
        ):
            agent = create_agent(
                base_url="http://fake:8080",
                model_settings=explicit,
                channel="whatsapp",
            )

        assert agent.model_settings is explicit
        assert "openai_reasoning_effort" not in agent.model_settings
        reasoning_effort.assert_not_called()

    def test_fact_check_settings_include_reasoning_and_sampling_parameters(self):
        """The Discord fact-check path uses the synchronous reasoning policy."""
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
        }
