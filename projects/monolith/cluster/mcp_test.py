"""Smoke test that all seven cluster MCP tools are registered."""

from __future__ import annotations

import importlib

from kubernetes_asyncio.client.exceptions import ApiException

import pytest

EXPECTED_TOOLS = {
    "k8s_health_summary",
    "k8s_list_resources",
    "k8s_get_resource",
    "k8s_get_pod_logs",
    "k8s_get_events",
    "k8s_sync_argocd_app",
    "verify_deployment",
}


@pytest.mark.asyncio
async def test_all_cluster_tools_registered():
    importlib.import_module("cluster.mcp")
    from core.mcp_app import mcp

    tools = await mcp.list_tools()
    registered = {t.name for t in tools}
    missing = EXPECTED_TOOLS - registered
    assert not missing, f"Missing cluster tools: {missing}"


def test_expected_tool_count_is_seven():
    """Guard against silently dropping a tool from EXPECTED_TOOLS."""
    assert len(EXPECTED_TOOLS) == 7


@pytest.mark.asyncio
async def test_verify_deployment_reads_the_application(monkeypatch):
    mod = importlib.import_module("cluster.mcp")
    seen = {}

    class _K8s:
        async def get_argocd_application(self, name, namespace="argocd"):
            seen["args"] = (name, namespace)
            if name == "missing":
                return None
            if name == "forbidden":
                raise ApiException(status=403)
            return {
                "metadata": {"name": name},
                "spec": {"source": {"chart": "x", "targetRevision": "1.2.3"}},
                "status": {
                    "sync": {"status": "Synced", "revision": "1.2.3"},
                    "health": {"status": "Healthy"},
                    "operationState": {
                        "phase": "Succeeded",
                        "syncResult": {"revision": "1.2.3"},
                    },
                },
            }

        async def close(self):
            seen["closed"] = True

    monkeypatch.setattr(mod, "KubernetesClient", _K8s)

    result = await mod.verify_deployment("monolith", expected_revision="1.2.0")
    assert seen["args"] == ("monolith", "argocd")
    assert seen["closed"]
    assert result["verdict"] == "verified"

    missing = await mod.verify_deployment("missing")
    assert "not found" in missing["error"]

    forbidden = await mod.verify_deployment("forbidden")
    assert "HTTP 403" in forbidden["error"]

    mismatch = await mod.verify_deployment("monolith", expected_revision="abcdef1")
    assert "cannot be compared" in mismatch["error"]


@pytest.mark.asyncio
async def test_verify_deployment_adds_kargo_context_for_kargo_owned_apps(monkeypatch):
    mod = importlib.import_module("cluster.mcp")
    seen = {}

    class _K8s:
        async def get_argocd_application(self, name, namespace="argocd"):
            return {
                "metadata": {
                    "name": name,
                    "annotations": {
                        "kargo.akuity.io/authorized-stage": "kargo-embervm:prod"
                    },
                },
                "spec": {"source": {"chart": "embervm", "targetRevision": "0.5.0"}},
                "status": {
                    "sync": {"status": "Synced", "revision": "0.5.0"},
                    "health": {"status": "Healthy"},
                    "operationState": {
                        "phase": "Succeeded",
                        "syncResult": {"revision": "0.5.0"},
                    },
                },
            }

        async def get_kargo_context(self, namespace, stage):
            seen["kargo"] = (namespace, stage)
            if seen.get("deny"):
                raise ApiException(status=403)
            return {
                "stage": {
                    "status": {
                        "currentPromotion": {
                            "name": "prod.0.6.0",
                            "freight": {
                                "charts": [
                                    {"repoURL": "oci://x/charts/embervm", "version": "0.6.0"}
                                ]
                            },
                            "status": {"phase": "Running", "currentStep": 1},
                        }
                    }
                },
                "freights": [],
            }

        async def close(self):
            seen["closed"] = True

    monkeypatch.setattr(mod, "KubernetesClient", _K8s)

    result = await mod.verify_deployment("embervm", expected_revision="0.6.0")
    assert seen["kargo"] == ("kargo-embervm", "prod")
    assert result["verdict"] == "in_progress"
    assert result["kargo"]["current_promotion"]["phase"] == "Running"

    seen["deny"] = True
    denied = await mod.verify_deployment("embervm")
    assert denied["verdict"] == "verified"
    assert "HTTP 403" in denied["kargo"]["error"]
