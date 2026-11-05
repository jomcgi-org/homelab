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

from chat import cluster_agent
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


class _RecordingClient:
    """Reject and record unknown access even when a handler swallows errors."""

    __slots__ = ("calls", "unexpected")

    def __init__(self):
        self.calls = []
        self.unexpected = []

    def __getattr__(self, name):
        self.unexpected.append(name)
        raise AssertionError(f"unexpected Kubernetes access: {name}")

    async def list_resources(self, kind, namespace=None, label_selector=None):
        self.calls.append(("list_resources", kind, namespace, label_selector))
        return [_POD] if kind == "pods" else []

    async def get_resource(self, kind, name, namespace=None):
        self.calls.append(("get_resource", kind, name, namespace))
        return _POD

    async def get_pod_logs(self, namespace, pod, container=None, tail_lines=200):
        self.calls.append(("get_pod_logs", namespace, pod, container, tail_lines))
        return "INFO starting\nERROR old\nINFO ready\nERROR retry\nERROR timeout"

    async def list_events(self, namespace=None, involved_object=None):
        self.calls.append(("list_events", namespace, involved_object))
        return [_EVENT, _EVENT]

    async def close(self):
        self.calls.append(("close",))


_POD = {
    "metadata": {"name": "worker-7", "namespace": "dispatch-test"},
    "spec": {"restartPolicy": "Always"},
    "status": {
        "phase": "Running",
        "containerStatuses": [{"ready": False, "restartCount": 3}],
    },
}
_POD_ROW = {
    "name": "worker-7",
    "namespace": "dispatch-test",
    "phase": "Running",
    "ready": "0/1",
    "restarts": 3,
    "restart_policy": "Always",
}
_EVENT = {
    "involvedObject": {
        "kind": "Pod",
        "name": "worker-7",
        "namespace": "dispatch-test",
    },
    "type": "Warning",
    "reason": "BackOff",
    "message": "retry worker",
    "count": 2,
    "firstTimestamp": "2026-11-01T01:00:00Z",
    "lastTimestamp": "2026-11-01T01:02:00Z",
}


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


class TestReadOnlyDispatch:
    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("tool_name", "args", "expected_calls", "expected_payload"),
        [
            (
                "health_summary",
                {},
                [
                    ("list_resources", kind, None, None)
                    for kind in cluster_agent._HEALTH_KINDS
                ],
                {
                    "healthy": False,
                    "scanned": 1,
                    "unhealthy": {"pods": [_POD_ROW]},
                },
            ),
            (
                "list_resources",
                {
                    "kind": "pods",
                    "namespace": "dispatch-test",
                    "label_selector": "app=worker,tier=batch",
                },
                [
                    (
                        "list_resources",
                        "pods",
                        "dispatch-test",
                        "app=worker,tier=batch",
                    )
                ],
                {"kind": "pods", "count": 1, "items": [_POD_ROW]},
            ),
            (
                "get_resource",
                {"kind": "pods", "name": "worker-7", "namespace": "dispatch-test"},
                [("get_resource", "pods", "worker-7", "dispatch-test")],
                {
                    "kind": "pods",
                    "name": "worker-7",
                    "namespace": "dispatch-test",
                    "status": {
                        key: value
                        for key, value in _POD_ROW.items()
                        if key not in ("name", "namespace")
                    },
                },
            ),
            (
                "pod_logs",
                {
                    "namespace": "dispatch-test",
                    "pod": "worker-7",
                    "container": "sidecar",
                    "tail_lines": 2,
                    "grep": "^ERROR",
                },
                [("get_pod_logs", "dispatch-test", "worker-7", "sidecar", 2)],
                {
                    "logs": "ERROR retry\nERROR timeout",
                    "lines": 2,
                    "filtered": True,
                    "truncated": True,
                },
            ),
            (
                "get_events",
                {"namespace": "dispatch-test", "involved_object": "worker-7"},
                [("list_events", "dispatch-test", "worker-7")],
                {
                    "count": 1,
                    "events": [
                        {
                            "object": "Pod/worker-7",
                            "namespace": "dispatch-test",
                            "type": "Warning",
                            "reason": "BackOff",
                            "message": "retry worker",
                            "count": 4,
                            "first_seen": "2026-11-01T01:00:00Z",
                            "last_seen": "2026-11-01T01:02:00Z",
                        }
                    ],
                },
            ),
        ],
        ids=[
            "health_summary",
            "list_resources",
            "get_resource",
            "pod_logs",
            "get_events",
        ],
    )
    async def test_registered_handler_reads_only(
        self, tool_name, args, expected_calls, expected_payload
    ):
        client = _RecordingClient()
        agent = create_cluster_agent()
        deps = ClusterDeps(emitter=SSEEmitter())
        with (
            patch.object(
                cluster_agent, "KubernetesClient", return_value=client
            ) as factory,
            agent.override(model=_one_tool_call_model(tool_name, args)),
        ):
            result = await agent.run("inspect the fixture", deps=deps)

        # Keep this assertion outside every production exception scope. In
        # particular, health_summary catches a forbidden access per kind.
        assert client.unexpected == [], (
            f"unexpected Kubernetes attempts: {client.unexpected}"
        )
        factory.assert_called_once_with()
        assert client.calls == [*expected_calls, ("close",)]
        returns = [
            part
            for message in result.all_messages()
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        assert len(returns) == 1
        assert returns[0].tool_name == tool_name
        assert returns[0].tool_call_id == "c1"
        assert json.loads(returns[0].content) == expected_payload


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
