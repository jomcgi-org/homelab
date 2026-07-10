"""Tests for tool registration in the PydanticAI chat agent."""

import pytest

from chat.agent import create_agent


class TestAgentToolRegistration:
    def test_search_history_tool_registered(self):
        """create_agent() registers 'search_history' as a callable tool."""
        agent = create_agent(base_url="http://fake:8080")
        tool_names = set(agent._function_toolset.tools.keys())
        assert "search_history" in tool_names

    def test_get_user_summary_tool_registered(self):
        """create_agent() registers 'get_user_summary' as a callable tool."""
        agent = create_agent(base_url="http://fake:8080")
        tool_names = set(agent._function_toolset.tools.keys())
        assert "get_user_summary" in tool_names

    def test_web_search_tool_registered(self):
        """create_agent() registers 'web_search' as a callable tool."""
        agent = create_agent(base_url="http://fake:8080")
        tool_names = set(agent._function_toolset.tools.keys())
        assert "web_search" in tool_names

    def test_all_three_tools_registered(self):
        """create_agent() registers exactly web_search, search_history, and get_user_summary."""
        agent = create_agent(base_url="http://fake:8080")
        tool_names = set(agent._function_toolset.tools.keys())
        expected_tools = {"web_search", "search_history", "get_user_summary"}
        assert expected_tools.issubset(tool_names)


class TestSignpostedDecorator:
    def test_attaches_signpost_attribute(self):
        """signposted decorator attaches .signpost to the function."""
        from chat.agent import signposted

        @signposted("test guidance")
        def dummy():
            pass

        assert dummy.signpost == "test guidance"

    def test_preserves_function_name(self):
        """signposted decorator preserves the original function name."""
        from chat.agent import signposted

        @signposted("test")
        def my_func():
            pass

        assert my_func.__name__ == "my_func"


class TestAllToolsSignposted:
    def test_all_tools_have_signposts(self):
        """Every registered tool must have a signpost."""
        agent = create_agent(base_url="http://fake:8080")
        for name, tool in agent._function_toolset.tools.items():
            signpost = getattr(tool.function, "signpost", None)
            assert signpost is not None, f"Tool '{name}' is missing a signpost"
            assert len(signpost) > 10, f"Tool '{name}' signpost is too short"


class TestToolSchemaSignposts:
    def test_agent_has_prepare_tools(self):
        """Agent is configured with a prepare_tools callback."""
        agent = create_agent(base_url="http://fake:8080")
        assert agent._prepare_tools is not None


class TestNoReplyTool:
    def test_no_reply_tool_registered(self):
        """create_agent() registers 'no_reply' as a callable tool."""
        agent = create_agent(base_url="http://fake:8080")
        tool_names = set(agent._function_toolset.tools.keys())
        assert "no_reply" in tool_names

    def test_no_reply_sets_flag_and_reason(self):
        """Calling no_reply flags the deps so the bot suppresses the post."""
        import asyncio
        from types import SimpleNamespace

        from chat.agent import ChatDeps

        agent = create_agent(base_url="http://fake:8080")
        fn = agent._function_toolset.tools["no_reply"].function
        deps = ChatDeps(channel_id="c1", store=None, embed_client=None)
        assert deps.no_reply_requested is False
        result = asyncio.run(fn(SimpleNamespace(deps=deps), reason="idle banter"))
        assert deps.no_reply_requested is True
        assert deps.no_reply_reason == "idle banter"
        assert isinstance(result, str)

    def test_no_reply_tolerates_missing_deps(self):
        """no_reply survives a deps-less run (tool unit tests, agent.run)."""
        import asyncio
        from types import SimpleNamespace

        agent = create_agent(base_url="http://fake:8080")
        fn = agent._function_toolset.tools["no_reply"].function
        result = asyncio.run(fn(SimpleNamespace(deps=None)))
        assert isinstance(result, str)
