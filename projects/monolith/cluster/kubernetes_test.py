"""Tests for the async Kubernetes client wrapper."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cluster.kubernetes import KubernetesClient, _parse_cpu, _parse_memory


@pytest.fixture
def k8s_client():
    return KubernetesClient()


def test_parse_cpu_handles_milli_and_plain():
    assert _parse_cpu("618m") == pytest.approx(0.618)
    assert _parse_cpu("16") == 16.0
    assert _parse_cpu("500u") == pytest.approx(0.0005)
    assert _parse_cpu("") == 0.0


def test_parse_memory_handles_binary_and_decimal_suffixes():
    assert _parse_memory("8309276Ki") == 8309276 * 1024
    assert _parse_memory("131072Mi") == 131072 * 1024**2
    assert _parse_memory("4Gi") == 4 * 1024**3
    assert _parse_memory("1000K") == 1_000_000
    assert _parse_memory("") == 0.0


@pytest.mark.asyncio
async def test_count_nodes(k8s_client):
    mock_api = MagicMock()
    mock_v1 = MagicMock()
    mock_v1.list_node = AsyncMock(
        return_value=MagicMock(items=[MagicMock(), MagicMock(), MagicMock()])
    )

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CoreV1Api", return_value=mock_v1),
    ):
        count = await k8s_client.count_nodes()

    assert count == 3


@pytest.mark.asyncio
async def test_count_argocd_applications(k8s_client):
    mock_api = MagicMock()
    mock_custom = MagicMock()
    mock_custom.list_namespaced_custom_object = AsyncMock(
        return_value={
            "items": [{"metadata": {"name": "app1"}}, {"metadata": {"name": "app2"}}]
        }
    )

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch(
            "cluster.kubernetes.client.CustomObjectsApi",
            return_value=mock_custom,
        ),
    ):
        count = await k8s_client.count_argocd_applications()

    assert count == 2


def _node(name: str, allocatable: dict) -> MagicMock:
    node = MagicMock()
    node.metadata.name = name
    node.status.allocatable = allocatable
    return node


def _summary_json(rss_mib: float) -> str:
    """A minimal kubelet /stats/summary payload carrying node rssBytes."""
    return json.dumps({"node": {"memory": {"rssBytes": rss_mib * 1024**2}}})


@pytest.mark.asyncio
async def test_aggregate_node_resources_uses_rss_not_working_set(k8s_client):
    mock_api = MagicMock()
    mock_v1 = MagicMock()
    mock_custom = MagicMock()

    nodes = [
        _node("node-a", {"cpu": "16", "memory": "65536Mi"}),
        _node("node-b", {"cpu": "8", "memory": "32768Mi"}),
    ]
    mock_v1.list_node = AsyncMock(return_value=MagicMock(items=nodes))

    # Metrics API reports inflated working-set memory (includes page cache).
    mock_custom.list_cluster_custom_object = AsyncMock(
        return_value={
            "items": [
                {
                    "metadata": {"name": "node-a"},
                    "usage": {"cpu": "1500m", "memory": "20480Mi"},
                },
                {
                    "metadata": {"name": "node-b"},
                    "usage": {"cpu": "500m", "memory": "10240Mi"},
                },
            ]
        }
    )

    # Kubelet Summary API reports honest rssBytes (excludes page cache).
    rss = {"node-a": 8192, "node-b": 4096}
    mock_v1.connect_get_node_proxy_with_path = AsyncMock(
        side_effect=lambda name, path: _summary_json(rss[name])
    )

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CoreV1Api", return_value=mock_v1),
        patch(
            "cluster.kubernetes.client.CustomObjectsApi",
            return_value=mock_custom,
        ),
    ):
        result = await k8s_client.aggregate_node_resources()

    assert result["cpu_used_cores"] == pytest.approx(2.0)
    assert result["cpu_capacity_cores"] == pytest.approx(24.0)
    # Memory = rss sum (12288Mi), NOT the working-set sum (30720Mi).
    assert result["memory_used_bytes"] == pytest.approx(12288 * 1024**2)
    assert result["memory_capacity_bytes"] == pytest.approx(98304 * 1024**2)


@pytest.mark.asyncio
async def test_aggregate_node_resources_falls_back_to_working_set(k8s_client):
    """When the kubelet Summary API is unavailable, per-node memory falls back
    to the metrics-server working-set value rather than dropping to zero."""
    mock_api = MagicMock()
    mock_v1 = MagicMock()
    mock_custom = MagicMock()

    nodes = [
        _node("node-a", {"cpu": "16", "memory": "65536Mi"}),
        _node("node-b", {"cpu": "8", "memory": "32768Mi"}),
    ]
    mock_v1.list_node = AsyncMock(return_value=MagicMock(items=nodes))
    mock_custom.list_cluster_custom_object = AsyncMock(
        return_value={
            "items": [
                {
                    "metadata": {"name": "node-a"},
                    "usage": {"cpu": "1500m", "memory": "20480Mi"},
                },
                {
                    "metadata": {"name": "node-b"},
                    "usage": {"cpu": "500m", "memory": "10240Mi"},
                },
            ]
        }
    )

    # node-a summary fails (-> working set 20480Mi); node-b returns rss 4096Mi.
    async def _proxy(name, path):
        if name == "node-a":
            raise RuntimeError("kubelet unreachable")
        return _summary_json(4096)

    mock_v1.connect_get_node_proxy_with_path = AsyncMock(side_effect=_proxy)

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CoreV1Api", return_value=mock_v1),
        patch(
            "cluster.kubernetes.client.CustomObjectsApi",
            return_value=mock_custom,
        ),
    ):
        result = await k8s_client.aggregate_node_resources()

    # 20480Mi (node-a working-set fallback) + 4096Mi (node-b rss) = 24576Mi.
    assert result["memory_used_bytes"] == pytest.approx(24576 * 1024**2)


@pytest.mark.asyncio
async def test_close_cleans_up(k8s_client):
    mock_api = MagicMock()
    mock_api.close = AsyncMock()

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
    ):
        # Force client creation
        await k8s_client._ensure_client()
        await k8s_client.close()

    mock_api.close.assert_called_once()
    assert k8s_client._api is None


@pytest.mark.asyncio
async def test_create_workflow_passes_correct_args_and_returns_name(k8s_client):
    mock_api = MagicMock()
    mock_custom = MagicMock()
    server_response = {"metadata": {"name": "my-workflow-xyz"}}
    mock_custom.create_namespaced_custom_object = AsyncMock(
        return_value=server_response
    )

    body = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Workflow",
        "metadata": {"generateName": "my-workflow-"},
        "spec": {"entrypoint": "main", "templates": []},
    }

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch(
            "cluster.kubernetes.client.CustomObjectsApi",
            return_value=mock_custom,
        ),
    ):
        name = await k8s_client.create_workflow(
            namespace="monolith-workflows", body=body
        )

    mock_custom.create_namespaced_custom_object.assert_called_once_with(
        group="argoproj.io",
        version="v1alpha1",
        namespace="monolith-workflows",
        plural="workflows",
        body=body,
    )
    assert name == "my-workflow-xyz"
