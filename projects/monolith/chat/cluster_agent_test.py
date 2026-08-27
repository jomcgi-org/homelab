"""Tests for chat.cluster_agent: tool registration, error wrapping, SSE events.

Exercises the agent's tool dispatch end-to-end with a scripted FunctionModel
(same technique as chat/explore_router_test.py), so tool calls run through the
real PydanticAI toolset instead of a hand-built RunContext.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from chat.cluster_agent import ClusterDeps, create_cluster_agent
from chat.sse import SSEEmitter


async def _drain(emitter: SSEEmitter) -> list[dict]:
    emitter.close()
    events = []
    async for chunk in emitter.stream():
        events.append(json.loads(chunk.removeprefix("data: ").strip()))
    return events


def _one_tool_call_model(tool_name: str, args: dict) -> FunctionModel:
    """A scripted model that calls one tool, then returns final text."""

    def _model(messages, info):
        returns = sum(
            1
            for msg in messages
            if hasattr(msg, "parts")
            for part in msg.parts
            if isinstance(part, ToolReturnPart)
        )
        if returns == 0:
            return ModelResponse(
                parts=[ToolCallPart(tool_name=tool_name, args=args, tool_call_id="c1")]
            )
        return ModelResponse(parts=[TextPart("done")])

    return FunctionModel(_model)


class TestToolRegistration:
    def test_agent_registers_five_read_only_tools(self):
        agent = create_cluster_agent()
        tool_names = set(agent._function_toolset.tools.keys())
        assert tool_names == {
            "health_summary",
            "list_resources",
            "get_resource",
            "pod_logs",
            "get_events",
        }

    def test_no_argocd_sync_tool_registered(self):
        # Read-only guarantee: the sync tool must never be wired into the
        # chat agent, only into the (separate) MCP surface.
        import inspect

        from chat import cluster_agent

        src = inspect.getsource(cluster_agent)
        assert "sync_argocd_app" not in src
        assert "k8s_sync_argocd_app" not in src


class TestErrorWrapping:
    @pytest.mark.anyio
    async def test_health_summary_survives_partial_list_failure(self):
        from chat import cluster_agent

        with patch.object(cluster_agent, "KubernetesClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.list_resources.side_effect = RuntimeError("api down")
            mock_cls.return_value = mock_client

            result = await cluster_agent._health_summary()

        # Per-kind list failures are swallowed inside build_health's caller,
        # so the summary call still returns a valid payload, not "error: ...".
        assert "healthy" in result

    @pytest.mark.anyio
    async def test_pod_logs_wraps_error(self):
        from chat import cluster_agent

        with patch.object(cluster_agent, "KubernetesClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get_pod_logs.side_effect = RuntimeError("pod not found")
            mock_cls.return_value = mock_client

            result = await cluster_agent._pod_logs("default", "web-0", None, 200, None)

        assert result == "error: pod not found"

    @pytest.mark.anyio
    async def test_get_resource_wraps_error(self):
        from chat import cluster_agent

        with patch.object(cluster_agent, "KubernetesClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get_resource.side_effect = RuntimeError("boom")
            mock_cls.return_value = mock_client

            result = await cluster_agent._get_resource("pods", "web-0", "default")

        assert result == "error: boom"

    @pytest.mark.anyio
    async def test_get_events_wraps_error(self):
        from chat import cluster_agent

        with patch.object(cluster_agent, "KubernetesClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.list_events.side_effect = RuntimeError("timeout")
            mock_cls.return_value = mock_client

            result = await cluster_agent._get_events(None, None)

        assert result == "error: timeout"

    @pytest.mark.anyio
    async def test_list_resources_wraps_unknown_kind(self):
        from chat import cluster_agent
        from cluster.api import UnknownKindError

        with patch.object(cluster_agent, "KubernetesClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.list_resources.side_effect = UnknownKindError("widgets")
            mock_cls.return_value = mock_client

            result = await cluster_agent._list_resources("widgets", None, None)

        assert result.startswith("error:")


class TestToolCallEvent:
    @pytest.mark.anyio
    async def test_health_summary_emits_tool_call_before_running(self):
        from chat import cluster_agent

        agent = create_cluster_agent()
        emitter = SSEEmitter()
        deps = ClusterDeps(emitter=emitter)

        with (
            patch.object(
                cluster_agent,
                "_health_summary",
                AsyncMock(return_value='{"healthy": true}'),
            ),
            agent.override(model=_one_tool_call_model("health_summary", {})),
        ):
            await agent.run("what's broken?", deps=deps)

        events = await _drain(emitter)
        tool_calls = [e for e in events if e["type"] == "tool_call"]
        assert len(tool_calls) == 1
        assert tool_calls[0]["data"]["tool"] == "health_summary"
        assert tool_calls[0]["data"]["args"] == {}

    @pytest.mark.anyio
    async def test_list_resources_emits_tool_call_with_args(self):
        from chat import cluster_agent

        agent = create_cluster_agent()
        emitter = SSEEmitter()
        deps = ClusterDeps(emitter=emitter)

        args = {"kind": "pods", "namespace": "default", "label_selector": None}
        with (
            patch.object(
                cluster_agent,
                "_list_resources",
                AsyncMock(return_value='{"kind": "pods", "count": 0, "items": []}'),
            ),
            agent.override(model=_one_tool_call_model("list_resources", args)),
        ):
            await agent.run("list pods", deps=deps)

        events = await _drain(emitter)
        tool_calls = [e for e in events if e["type"] == "tool_call"]
        assert len(tool_calls) == 1
        assert tool_calls[0]["data"]["tool"] == "list_resources"
        assert tool_calls[0]["data"]["args"]["kind"] == "pods"
        assert tool_calls[0]["data"]["args"]["namespace"] == "default"
