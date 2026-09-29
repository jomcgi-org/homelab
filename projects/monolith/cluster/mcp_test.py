"""Smoke test that all eight cluster MCP tools are registered."""

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
    "kargo_promote",
}


@pytest.mark.asyncio
async def test_all_cluster_tools_registered():
    importlib.import_module("cluster.mcp")
    from core.mcp_app import mcp

    tools = await mcp.list_tools()
    registered = {t.name for t in tools}
    missing = EXPECTED_TOOLS - registered
    assert not missing, f"Missing cluster tools: {missing}"


def test_expected_tool_count_is_eight():
    """Guard against silently dropping a tool from EXPECTED_TOOLS."""
    assert len(EXPECTED_TOOLS) == 8


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
                                    {
                                        "repoURL": "oci://x/charts/embervm",
                                        "version": "0.6.0",
                                    }
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


def _kargo_stage(current=None):
    return {
        "metadata": {"name": "prod"},
        "spec": {
            "requestedFreight": [{"sources": {"stages": ["dev"]}}],
            "promotionTemplate": {
                "spec": {"steps": [{"uses": "argocd-update"}], "vars": [{"name": "v"}]}
            },
        },
        "status": {"currentPromotion": current} if current else {},
    }


def _kargo_freight(verified=("dev",)):
    return {
        "metadata": {"name": "abc123"},
        "alias": "brave-otter",
        "charts": [{"repoURL": "oci://x/charts/embervm", "version": "0.6.0"}],
        "status": {"verifiedIn": {s: {} for s in verified}},
    }


def _promote_k8s(seen, stage, freights, annotated=True):
    class _K8s:
        async def get_argocd_application(self, name, namespace="argocd"):
            if name == "missing":
                return None
            annotations = (
                {"kargo.akuity.io/authorized-stage": "kargo-embervm:prod"}
                if annotated
                else {}
            )
            return {
                "metadata": {"name": name, "annotations": annotations},
                "spec": {"source": {"chart": "embervm", "targetRevision": "0.5.0"}},
            }

        async def get_kargo_context(self, namespace, stage_name):
            return {"stage": stage, "freights": freights}

        async def create_kargo_promotion(self, namespace, body, dry_run=False):
            if seen.get("deny"):
                raise ApiException(status=403, reason="Forbidden")
            seen["created"] = (namespace, body, dry_run)
            return {"metadata": {"name": "prod.01abc.abc123"}}

        async def close(self):
            seen["closed"] = True

    return _K8s


@pytest.mark.asyncio
async def test_kargo_promote_creates_the_stage_template_promotion(monkeypatch):
    mod = importlib.import_module("cluster.mcp")
    seen = {}
    monkeypatch.setattr(
        mod, "KubernetesClient", _promote_k8s(seen, _kargo_stage(), [_kargo_freight()])
    )
    result = await mod.kargo_promote("embervm", "0.6.0", dry_run=True)
    assert result == {
        "app": "embervm",
        "namespace": "kargo-embervm",
        "stage": "prod",
        "freight": "abc123",
        "chart_version": "0.6.0",
        "promotion": "prod.01abc.abc123",
        "dry_run": True,
    }
    namespace, body, dry_run = seen["created"]
    assert namespace == "kargo-embervm" and dry_run is True
    assert body["spec"] == {
        "stage": "prod",
        "freight": "abc123",
        "steps": [{"uses": "argocd-update"}],
        "vars": [{"name": "v"}],
    }
    assert seen["closed"] is True


@pytest.mark.asyncio
async def test_kargo_promote_refuses_and_reports_errors(monkeypatch):
    mod = importlib.import_module("cluster.mcp")
    seen = {}

    def use(stage, freights, annotated=True):
        monkeypatch.setattr(
            mod, "KubernetesClient", _promote_k8s(seen, stage, freights, annotated)
        )

    use(_kargo_stage(), [_kargo_freight()])
    assert "not found" in (await mod.kargo_promote("missing", "0.6.0"))["error"]

    use(_kargo_stage(), [_kargo_freight()], annotated=False)
    assert "not promoted by Kargo" in (await mod.kargo_promote("x", "0.6.0"))["error"]

    use(_kargo_stage(current={"name": "prod.running"}), [_kargo_freight()])
    error = (await mod.kargo_promote("embervm", "0.6.0"))["error"]
    assert "prod.running is still running" in error

    use(_kargo_stage(), [_kargo_freight(verified=())])
    error = (await mod.kargo_promote("embervm", "0.6.0"))["error"]
    assert error.startswith("not promoting: Freight brave-otter")

    use(_kargo_stage(), [_kargo_freight()])
    seen["deny"] = True
    error = (await mod.kargo_promote("embervm", "0.6.0"))["error"]
    assert error == "promotion failed: HTTP 403: Forbidden"
    assert "created" not in seen
