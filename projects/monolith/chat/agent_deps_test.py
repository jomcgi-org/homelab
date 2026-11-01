"""Tests for ChatDeps and agent dependency injection."""

from unittest.mock import AsyncMock, MagicMock

from chat.agent import ChatDeps, create_agent


class TestChatDeps:
    def test_creates_deps_instance(self):
        """ChatDeps holds channel_id, store, and embed_client."""
        deps = ChatDeps(
            channel_id="ch1",
            store=MagicMock(),
            embed_client=AsyncMock(),
        )
        assert deps.channel_id == "ch1"


class TestCreateAgentWithDeps:
    def test_agent_creates_successfully(self):
        """create_agent returns an Agent parameterized with ChatDeps."""
        agent = create_agent(base_url="http://fake:8080")
        assert agent is not None
