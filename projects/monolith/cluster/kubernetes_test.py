"""Tests for the async Kubernetes client wrapper."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from cluster.kubernetes import (
    KubernetesClient,
    UnknownKindError,
    _parse_cpu,
    _parse_memory,
)


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


@pytest.mark.asyncio
async def test_list_kargo_freight_parses_real_shape(k8s_client):
    mock_api = MagicMock()
    mock_custom = MagicMock()
    mock_custom.list_namespaced_custom_object = AsyncMock(
        return_value={
            "items": [
                {
                    "metadata": {
                        "name": "freight-123",
                        "creationTimestamp": "2026-08-13T10:00:00Z",
                    },
                    "charts": [
                        {
                            "repoURL": "oci://ghcr.io/jomcgi/homelab/charts/monolith",
                            "version": "0.301.1",
                        }
                    ],
                }
            ]
        }
    )

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CustomObjectsApi", return_value=mock_custom),
    ):
        result = await k8s_client.list_kargo_freight()
    assert result == [
        {
            "name": "freight-123",
            "version": "0.301.1",
            "created_at": "2026-08-13T10:00:00Z",
        }
    ]


@pytest.mark.asyncio
async def test_list_kargo_freight_skips_non_monolith_and_missing_charts(k8s_client):
    mock_api = MagicMock()
    mock_custom = MagicMock()
    mock_custom.list_namespaced_custom_object = AsyncMock(
        return_value={
            "items": [
                {
                    "metadata": {"name": "other"},
                    "charts": [
                        {"repoURL": "oci://example/charts/other", "version": "1.0.0"}
                    ],
                },
                {"metadata": {"name": "missing"}},
            ]
        }
    )
    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CustomObjectsApi", return_value=mock_custom),
    ):
        assert await k8s_client.list_kargo_freight() == []


async def _deployed_revision(k8s_client, result):
    mock_api = MagicMock()
    mock_custom = MagicMock()
    mock_custom.get_namespaced_custom_object = AsyncMock(return_value=result)
    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CustomObjectsApi", return_value=mock_custom),
    ):
        return await k8s_client.get_argocd_app_deployed_revision("monolith")


@pytest.mark.asyncio
async def test_deployed_revision_prefers_status_over_spec(k8s_client):
    assert (
        await _deployed_revision(
            k8s_client,
            {
                "status": {
                    "sync": {
                        "revisions": [
                            "0.301.1",
                            "7a0c8c25c48fbb65d18b62f4e93fc4231629f8a0",
                        ]
                    }
                },
                "spec": {"sources": [{"targetRevision": "0.302.0"}]},
            },
        )
        == "0.301.1"
    )


@pytest.mark.asyncio
async def test_deployed_revision_falls_back_to_spec(k8s_client):
    assert (
        await _deployed_revision(
            k8s_client, {"spec": {"sources": [{"targetRevision": "0.302.0"}]}}
        )
        == "0.302.0"
    )


@pytest.mark.asyncio
async def test_deployed_revision_handles_single_source_legacy_shape(k8s_client):
    assert (
        await _deployed_revision(
            k8s_client,
            {
                "status": {"sync": {"revision": "0.301.1"}},
                "spec": {"sources": [], "source": {"targetRevision": "0.300.9"}},
            },
        )
        == "0.301.1"
    )
    assert (
        await _deployed_revision(
            k8s_client,
            {"spec": {"sources": [], "source": {"targetRevision": "0.300.9"}}},
        )
        == "0.300.9"
    )


@pytest.mark.asyncio
async def test_deployed_revision_returns_none_on_404(k8s_client):
    mock_api = MagicMock()
    mock_custom = MagicMock()
    mock_custom.get_namespaced_custom_object = AsyncMock(
        side_effect=ApiException(status=404)
    )
    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CustomObjectsApi", return_value=mock_custom),
    ):
        assert await k8s_client.get_argocd_app_deployed_revision("missing") is None


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


# ---------------------------------------------------------------------------
# count_pods
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_pods_calls_list_pod_for_all_namespaces_and_returns_len(k8s_client):
    mock_api = MagicMock()
    mock_v1 = MagicMock()
    mock_v1.list_pod_for_all_namespaces = AsyncMock(
        return_value=MagicMock(items=[MagicMock(), MagicMock()])
    )

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CoreV1Api", return_value=mock_v1),
    ):
        count = await k8s_client.count_pods()

    mock_v1.list_pod_for_all_namespaces.assert_called_once()
    assert count == 2


# ---------------------------------------------------------------------------
# count_deployments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_deployments_calls_list_deployment_for_all_namespaces_and_returns_len(
    k8s_client,
):
    mock_api = MagicMock()
    mock_apps = MagicMock()
    mock_apps.list_deployment_for_all_namespaces = AsyncMock(
        return_value=MagicMock(items=[MagicMock(), MagicMock(), MagicMock()])
    )

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.AppsV1Api", return_value=mock_apps),
    ):
        count = await k8s_client.count_deployments()

    mock_apps.list_deployment_for_all_namespaces.assert_called_once()
    assert count == 3


# ---------------------------------------------------------------------------
# get_argocd_app_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_argocd_app_status_returns_status_dict(k8s_client):
    mock_api = MagicMock()
    mock_custom = MagicMock()
    status = {"health": {"status": "Healthy"}, "sync": {"status": "Synced"}}
    mock_custom.get_namespaced_custom_object = AsyncMock(
        return_value={"metadata": {"name": "my-app"}, "status": status}
    )

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CustomObjectsApi", return_value=mock_custom),
    ):
        result = await k8s_client.get_argocd_app_status("my-app")

    mock_custom.get_namespaced_custom_object.assert_called_once_with(
        group="argoproj.io",
        version="v1alpha1",
        namespace="argocd",
        plural="applications",
        name="my-app",
    )
    assert result == status


@pytest.mark.asyncio
async def test_get_argocd_app_status_returns_none_on_api_exception(k8s_client):
    mock_api = MagicMock()
    mock_custom = MagicMock()
    mock_custom.get_namespaced_custom_object = AsyncMock(
        side_effect=ApiException(status=404, reason="Not Found")
    )

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CustomObjectsApi", return_value=mock_custom),
    ):
        result = await k8s_client.get_argocd_app_status("missing-app")

    assert result is None


# ---------------------------------------------------------------------------
# list_resources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_resources_applications_uses_custom_objects_api(k8s_client):
    mock_api = MagicMock()
    mock_custom = MagicMock()
    app_items = [{"metadata": {"name": "app1"}}, {"metadata": {"name": "app2"}}]
    mock_custom.list_namespaced_custom_object = AsyncMock(
        return_value={"items": app_items}
    )

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CustomObjectsApi", return_value=mock_custom),
    ):
        result = await k8s_client.list_resources("applications")

    mock_custom.list_namespaced_custom_object.assert_called_once_with(
        group="argoproj.io",
        version="v1alpha1",
        namespace="argocd",
        plural="applications",
        label_selector=None,
    )
    assert result == app_items


@pytest.mark.asyncio
async def test_list_resources_namespaced_kind_with_namespace(k8s_client):
    mock_api = MagicMock()
    mock_v1 = MagicMock()
    pod_items = [MagicMock(), MagicMock()]
    mock_v1.list_namespaced_pod = AsyncMock(return_value=MagicMock(items=pod_items))
    mock_api.sanitize_for_serialization = MagicMock(side_effect=lambda x: {"pod": True})

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CoreV1Api", return_value=mock_v1),
    ):
        result = await k8s_client.list_resources("pods", namespace="default")

    mock_v1.list_namespaced_pod.assert_called_once_with("default")
    assert len(result) == 2


@pytest.mark.asyncio
async def test_list_resources_namespaced_kind_without_namespace_uses_all_namespaces(
    k8s_client,
):
    mock_api = MagicMock()
    mock_v1 = MagicMock()
    pod_items = [MagicMock()]
    mock_v1.list_pod_for_all_namespaces = AsyncMock(
        return_value=MagicMock(items=pod_items)
    )
    mock_api.sanitize_for_serialization = MagicMock(side_effect=lambda x: {"pod": True})

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CoreV1Api", return_value=mock_v1),
    ):
        result = await k8s_client.list_resources("pods")

    mock_v1.list_pod_for_all_namespaces.assert_called_once()
    assert len(result) == 1


@pytest.mark.asyncio
async def test_list_resources_cluster_scoped_kind_ignores_namespace(k8s_client):
    mock_api = MagicMock()
    mock_v1 = MagicMock()
    node_items = [MagicMock(), MagicMock(), MagicMock()]
    mock_v1.list_node = AsyncMock(return_value=MagicMock(items=node_items))
    mock_api.sanitize_for_serialization = MagicMock(
        side_effect=lambda x: {"node": True}
    )

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CoreV1Api", return_value=mock_v1),
    ):
        result = await k8s_client.list_resources("nodes", namespace="some-ns")

    mock_v1.list_node.assert_called_once()
    assert len(result) == 3


@pytest.mark.asyncio
async def test_list_resources_unknown_kind_raises(k8s_client):
    mock_api = MagicMock()

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
    ):
        with pytest.raises(UnknownKindError):
            await k8s_client.list_resources("fluxcapacitors")


# ---------------------------------------------------------------------------
# get_resource
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_resource_applications_returns_custom_object(k8s_client):
    mock_api = MagicMock()
    mock_custom = MagicMock()
    app_obj = {"metadata": {"name": "my-app"}, "spec": {}}
    mock_custom.get_namespaced_custom_object = AsyncMock(return_value=app_obj)

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CustomObjectsApi", return_value=mock_custom),
    ):
        result = await k8s_client.get_resource("applications", "my-app")

    mock_custom.get_namespaced_custom_object.assert_called_once_with(
        group="argoproj.io",
        version="v1alpha1",
        namespace="argocd",
        plural="applications",
        name="my-app",
    )
    assert result == app_obj


@pytest.mark.asyncio
async def test_get_resource_applications_returns_none_on_api_exception(k8s_client):
    mock_api = MagicMock()
    mock_custom = MagicMock()
    mock_custom.get_namespaced_custom_object = AsyncMock(
        side_effect=ApiException(status=404, reason="Not Found")
    )

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CustomObjectsApi", return_value=mock_custom),
    ):
        result = await k8s_client.get_resource("applications", "no-such-app")

    assert result is None


@pytest.mark.asyncio
async def test_get_resource_namespaced_kind_hit(k8s_client):
    mock_api = MagicMock()
    mock_v1 = MagicMock()
    pod_obj = MagicMock()
    mock_v1.read_namespaced_pod = AsyncMock(return_value=pod_obj)
    mock_api.sanitize_for_serialization = MagicMock(return_value={"kind": "Pod"})

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CoreV1Api", return_value=mock_v1),
    ):
        result = await k8s_client.get_resource(
            "pods", "my-pod", namespace="kube-system"
        )

    mock_v1.read_namespaced_pod.assert_called_once_with("my-pod", "kube-system")
    assert result == {"kind": "Pod"}


@pytest.mark.asyncio
async def test_get_resource_namespaced_kind_miss_returns_none(k8s_client):
    mock_api = MagicMock()
    mock_v1 = MagicMock()
    mock_v1.read_namespaced_pod = AsyncMock(
        side_effect=ApiException(status=404, reason="Not Found")
    )

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CoreV1Api", return_value=mock_v1),
    ):
        result = await k8s_client.get_resource("pods", "ghost-pod")

    assert result is None


@pytest.mark.asyncio
async def test_get_resource_cluster_scoped_kind(k8s_client):
    mock_api = MagicMock()
    mock_v1 = MagicMock()
    node_obj = MagicMock()
    mock_v1.read_node = AsyncMock(return_value=node_obj)
    mock_api.sanitize_for_serialization = MagicMock(return_value={"kind": "Node"})

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CoreV1Api", return_value=mock_v1),
    ):
        result = await k8s_client.get_resource("nodes", "worker-1")

    mock_v1.read_node.assert_called_once_with("worker-1")
    assert result == {"kind": "Node"}


@pytest.mark.asyncio
async def test_get_resource_unknown_kind_raises(k8s_client):
    mock_api = MagicMock()

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
    ):
        with pytest.raises(UnknownKindError):
            await k8s_client.get_resource("widgets", "my-widget")


# ---------------------------------------------------------------------------
# get_pod_logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_pod_logs_passes_all_args_to_read_namespaced_pod_log(k8s_client):
    mock_api = MagicMock()
    mock_v1 = MagicMock()
    mock_v1.read_namespaced_pod_log = AsyncMock(return_value="log line 1\nlog line 2\n")

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CoreV1Api", return_value=mock_v1),
    ):
        logs = await k8s_client.get_pod_logs(
            namespace="prod",
            name="my-pod",
            container="app",
            tail_lines=50,
            since_seconds=300,
            previous=True,
        )

    mock_v1.read_namespaced_pod_log.assert_called_once_with(
        name="my-pod",
        namespace="prod",
        container="app",
        tail_lines=50,
        since_seconds=300,
        previous=True,
        timestamps=True,
    )
    assert logs == "log line 1\nlog line 2\n"


@pytest.mark.asyncio
async def test_get_pod_logs_uses_default_tail_lines(k8s_client):
    mock_api = MagicMock()
    mock_v1 = MagicMock()
    mock_v1.read_namespaced_pod_log = AsyncMock(return_value="")

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CoreV1Api", return_value=mock_v1),
    ):
        await k8s_client.get_pod_logs(namespace="default", name="my-pod")

    _, kwargs = mock_v1.read_namespaced_pod_log.call_args
    assert kwargs["tail_lines"] == 200
    assert kwargs["container"] is None
    assert kwargs["since_seconds"] is None
    assert kwargs["previous"] is False
    assert kwargs["timestamps"] is True


# ---------------------------------------------------------------------------
# list_events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_events_namespaced_with_involved_object(k8s_client):
    mock_api = MagicMock()
    mock_v1 = MagicMock()
    event_items = [MagicMock(), MagicMock()]
    mock_v1.list_namespaced_event = AsyncMock(return_value=MagicMock(items=event_items))
    mock_api.sanitize_for_serialization = MagicMock(
        side_effect=lambda x: {"event": True}
    )

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CoreV1Api", return_value=mock_v1),
    ):
        result = await k8s_client.list_events(
            namespace="prod", involved_object="my-pod"
        )

    mock_v1.list_namespaced_event.assert_called_once_with(
        "prod", field_selector="involvedObject.name=my-pod"
    )
    assert len(result) == 2


@pytest.mark.asyncio
async def test_list_events_all_namespaces_without_involved_object(k8s_client):
    mock_api = MagicMock()
    mock_v1 = MagicMock()
    event_items = [MagicMock()]
    mock_v1.list_event_for_all_namespaces = AsyncMock(
        return_value=MagicMock(items=event_items)
    )
    mock_api.sanitize_for_serialization = MagicMock(
        side_effect=lambda x: {"event": True}
    )

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CoreV1Api", return_value=mock_v1),
    ):
        result = await k8s_client.list_events()

    mock_v1.list_event_for_all_namespaces.assert_called_once_with(field_selector=None)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# sync_argocd_app
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_argocd_app_patches_correct_body_and_returns_dict(k8s_client):
    mock_api = MagicMock()
    mock_custom = MagicMock()
    mock_custom.patch_namespaced_custom_object = AsyncMock(return_value={})

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CustomObjectsApi", return_value=mock_custom),
    ):
        result = await k8s_client.sync_argocd_app("my-app", prune=True, dry_run=False)

    expected_body = {
        "operation": {
            "initiatedBy": {"username": "monolith-k8s-mcp"},
            "sync": {"prune": True, "dryRun": False},
        }
    }
    mock_custom.patch_namespaced_custom_object.assert_called_once_with(
        group="argoproj.io",
        version="v1alpha1",
        namespace="argocd",
        plural="applications",
        name="my-app",
        body=expected_body,
        _content_type="application/merge-patch+json",
    )
    assert result == {"app": "my-app", "synced": True, "prune": True, "dry_run": False}


@pytest.mark.asyncio
async def test_sync_argocd_app_dry_run_mode(k8s_client):
    mock_api = MagicMock()
    mock_custom = MagicMock()
    mock_custom.patch_namespaced_custom_object = AsyncMock(return_value={})

    with (
        patch("cluster.kubernetes.config.load_incluster_config"),
        patch("cluster.kubernetes.ApiClient", return_value=mock_api),
        patch("cluster.kubernetes.client.CustomObjectsApi", return_value=mock_custom),
    ):
        result = await k8s_client.sync_argocd_app(
            "my-app", namespace="custom-ns", prune=False, dry_run=True
        )

    _, kwargs = mock_custom.patch_namespaced_custom_object.call_args
    assert kwargs["namespace"] == "custom-ns"
    assert kwargs["body"]["operation"]["sync"]["dryRun"] is True
    assert kwargs["body"]["operation"]["sync"]["prune"] is False
    assert result["dry_run"] is True
    assert result["prune"] is False
