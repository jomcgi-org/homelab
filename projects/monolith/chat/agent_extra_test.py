"""Extra coverage for agent.py -- empty message list and web_search tool body."""

from unittest.mock import patch

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from chat.agent import create_agent, format_context_messages


class TestFormatContextMessagesEmpty:
    def test_empty_list_returns_empty_string(self):
        """format_context_messages([]) returns an empty string, not an error."""
        result = format_context_messages([])
        assert result == ""


class TestWebSearchToolBody:
    @pytest.mark.asyncio
    async def test_web_search_tool_delegates_to_search_web(self):
        """The web_search tool registered on the agent calls search_web() when invoked."""
        tool_was_called = []

        async def fake_search_web(query: str, base_url: str | None = None) -> str:
            tool_was_called.append(query)
            return "mocked search result"

        def model_func(messages, info):  # type: ignore[type-arg]
            # Check if there is a tool return in the history (i.e. tool was already called).
            from pydantic_ai.messages import ToolReturnPart

            for msg in messages:
                if hasattr(msg, "parts"):
                    for part in msg.parts:
                        if isinstance(part, ToolReturnPart):
                            # Tool was executed — now return the final text
                            return ModelResponse(parts=[TextPart("done")])
            # First call — request the web_search tool
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="web_search",
                        args={"query": "test query"},
                        tool_call_id="call-1",
                    )
                ]
            )

        agent = create_agent(base_url="http://fake:8080")

        with patch("chat.agent.search_web", side_effect=fake_search_web):
            result = await agent.run(
                "What is the news?",
                model=FunctionModel(model_func),
            )

        assert len(tool_was_called) == 1
        assert tool_was_called[0] == "test query"
        assert result.output == "done"
