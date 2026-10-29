"""Cluster MCP tools: registration, verdicts, promotion, and mutation attribution."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from kubernetes_asyncio.client.exceptions import ApiException

import pytest

from auth.api import Authority, Principal, PrincipalKind, anonymous_principal
from auth.dependencies import reset_current_principal, set_current_principal

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


def _promote_k8s(seen, stage, freights, annotated=True, promotions=()):
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

        async def list_kargo_promotions(self, namespace):
            return list(promotions)

        async def create_kargo_promotion(self, namespace, body, dry_run=False):
            if seen.get("deny"):
                exc = ApiException(status=403, reason="Forbidden")
                exc.body = '{"message": "not authorized to promote to Stage prod"}'
                raise exc
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

    def use(stage, freights, annotated=True, promotions=()):
        monkeypatch.setattr(
            mod,
            "KubernetesClient",
            _promote_k8s(seen, stage, freights, annotated, promotions),
        )

    use(_kargo_stage(), [_kargo_freight()])
    assert "not found" in (await mod.kargo_promote("missing", "0.6.0"))["error"]

    use(_kargo_stage(), [_kargo_freight()], annotated=False)
    assert "not promoted by Kargo" in (await mod.kargo_promote("x", "0.6.0"))["error"]

    use(_kargo_stage(current={"name": "prod.running"}), [_kargo_freight()])
    error = (await mod.kargo_promote("embervm", "0.6.0"))["error"]
    assert "prod.running is still running" in error

    queued = {
        "metadata": {"name": "prod.queued"},
        "spec": {"stage": "prod"},
        "status": {"phase": "Pending"},
    }
    use(_kargo_stage(), [_kargo_freight()], promotions=[queued])
    error = (await mod.kargo_promote("embervm", "0.6.0"))["error"]
    assert "prod.queued is still running or queued" in error

    use(_kargo_stage(), [_kargo_freight(verified=())])
    error = (await mod.kargo_promote("embervm", "0.6.0"))["error"]
    assert error.startswith("not promoting: Freight brave-otter")

    use(_kargo_stage(), [_kargo_freight()])
    seen["deny"] = True
    error = (await mod.kargo_promote("embervm", "0.6.0"))["error"]
    assert error == (
        "promotion failed: HTTP 403: not authorized to promote to Stage prod"
    )
    assert "created" not in seen


@pytest.mark.asyncio
async def test_verify_deployment_keeps_its_verdict_when_kargo_read_times_out(
    monkeypatch,
):
    mod = importlib.import_module("cluster.mcp")

    class _K8s(_promote_k8s({}, None, [])):
        async def get_argocd_application(self, name, namespace="argocd"):
            app = await super().get_argocd_application(name, namespace)
            app["status"] = {
                "sync": {"status": "Synced", "revision": "0.5.0"},
                "health": {"status": "Healthy"},
            }
            return app

        async def get_kargo_context(self, namespace, stage_name):
            raise TimeoutError("read timed out")

    monkeypatch.setattr(mod, "KubernetesClient", _K8s)
    result = await mod.verify_deployment("embervm")
    assert result["verdict"] == "verified"
    assert "read timed out" in result["kargo"]["error"]


# ---------------------------------------------------------------------------
# Attribution of the two mutations to the verified request principal (#6476)
# ---------------------------------------------------------------------------


def _principal(subject, groups=("operators",)):
    return Principal(
        subject=subject,
        actor=(),
        scope=(),
        groups=groups,
        email=f"{subject}@example.test",
        kind=PrincipalKind.HUMAN,
        authority=Authority.STANDING,
        display_name=f"Display {subject}",
        username=f"user-{subject}",
    )


@contextlib.contextmanager
def _as(principal):
    """Stand in for PrincipalMiddleware: set the request principal, reset after."""
    token = set_current_principal(principal)
    try:
        yield
    finally:
        reset_current_principal(token)


def _patching_k8s(mod, monkeypatch, patch_mock):
    """The real KubernetesClient, with only the API client seam faked.

    The tool therefore builds the real outgoing PATCH body, so the tests assert
    what would reach the API server rather than what a fake chose to record.
    """
    from cluster.kubernetes import KubernetesClient

    closed = []

    class _K8s(KubernetesClient):
        async def _ensure_client(self):
            return MagicMock()

        async def close(self):
            closed.append(True)

    custom = MagicMock()
    custom.patch_namespaced_custom_object = patch_mock
    monkeypatch.setattr(mod, "KubernetesClient", _K8s)
    return custom, closed


def _bodies(patch_mock):
    return [c.kwargs["body"] for c in patch_mock.await_args_list]


@pytest.mark.asyncio
async def test_sync_records_each_verified_subject_as_initiator(monkeypatch, caplog):
    mod = importlib.import_module("cluster.mcp")
    patch_mock = AsyncMock(return_value={})
    custom, closed = _patching_k8s(mod, monkeypatch, patch_mock)
    caplog.set_level(logging.INFO, logger="cluster.mcp")

    with patch("cluster.kubernetes.client.CustomObjectsApi", return_value=custom):
        with _as(_principal("subject-alice")):
            alice = await mod.k8s_sync_argocd_app("my-app", prune=True)
        with _as(_principal("subject-bob")):
            bob = await mod.k8s_sync_argocd_app("other-app", dry_run=True)

    assert alice == {"app": "my-app", "synced": True, "prune": True, "dry_run": False}
    assert bob == {"app": "other-app", "synced": True, "prune": False, "dry_run": True}
    assert _bodies(patch_mock) == [
        {
            "operation": {
                "initiatedBy": {"username": "subject-alice"},
                "sync": {"prune": True, "dryRun": False},
            }
        },
        {
            "operation": {
                "initiatedBy": {"username": "subject-bob"},
                "sync": {"prune": False, "dryRun": True},
            }
        },
    ]
    for call in patch_mock.await_args_list:
        assert call.kwargs["namespace"] == "argocd"
        assert call.kwargs["_content_type"] == "application/merge-patch+json"
    assert closed == [True, True]

    lines = [r.getMessage() for r in caplog.records if r.name == "cluster.mcp"]
    assert lines == [
        "k8s_sync_argocd_app: sync of argocd/my-app requested by "
        "subject=subject-alice authority=standing (prune=True, dry_run=False)",
        "k8s_sync_argocd_app: sync of argocd/other-app requested by "
        "subject=subject-bob authority=standing (prune=False, dry_run=True)",
    ]
    # Subject, not email, username or display name.
    assert not any("example.test" in line or "Display" in line for line in lines)
    assert not any("user-subject" in line for line in lines)


@pytest.mark.asyncio
async def test_failed_sync_is_not_logged_as_requested(monkeypatch, caplog):
    mod = importlib.import_module("cluster.mcp")
    patch_mock = AsyncMock(side_effect=ApiException(status=403, reason="Forbidden"))
    custom, closed = _patching_k8s(mod, monkeypatch, patch_mock)
    caplog.set_level(logging.INFO, logger="cluster.mcp")

    with patch("cluster.kubernetes.client.CustomObjectsApi", return_value=custom):
        with _as(_principal("subject-alice")):
            result = await mod.k8s_sync_argocd_app("my-app")

    assert result["error"].startswith("sync failed: (403)")
    assert closed == [True]
    assert not [r for r in caplog.records if "requested by" in r.getMessage()]


@pytest.mark.asyncio
async def test_anonymous_sync_is_recorded_as_anonymous_not_an_operator(
    monkeypatch, caplog
):
    """Only reachable with the policy escape hatch off; recorded truthfully."""
    mod = importlib.import_module("cluster.mcp")
    patch_mock = AsyncMock(return_value={})
    custom, _ = _patching_k8s(mod, monkeypatch, patch_mock)
    caplog.set_level(logging.INFO, logger="cluster.mcp")

    with patch("cluster.kubernetes.client.CustomObjectsApi", return_value=custom):
        # No principal set at all: the ContextVar default.
        await mod.k8s_sync_argocd_app("my-app")
        # An earlier caller's context must not leak into a later one.
        with _as(_principal("subject-alice")):
            pass
        await mod.k8s_sync_argocd_app("my-app")
        with _as(anonymous_principal()):
            await mod.k8s_sync_argocd_app("my-app")

    usernames = [b["operation"]["initiatedBy"]["username"] for b in _bodies(patch_mock)]
    assert usernames == ["anonymous", "anonymous", "anonymous"]
    lines = [r.getMessage() for r in caplog.records if "requested by" in r.getMessage()]
    assert len(lines) == 3
    assert all("subject=anonymous authority=anonymous" in line for line in lines)


async def _as_caller(subject, call):
    with _as(_principal(subject)):
        return await call()


@pytest.mark.asyncio
async def test_interleaved_syncs_keep_their_own_subjects(monkeypatch):
    """Identity is read per request context, never shared across tasks."""
    mod = importlib.import_module("cluster.mcp")
    started, release = asyncio.Event(), asyncio.Event()
    bodies = {}

    async def slow_patch(**kwargs):
        # The first request parks mid-call while the second runs to completion.
        if kwargs["name"] == "first":
            started.set()
            await release.wait()
        bodies[kwargs["name"]] = kwargs["body"]
        return {}

    custom, _ = _patching_k8s(mod, monkeypatch, AsyncMock(side_effect=slow_patch))
    with patch("cluster.kubernetes.client.CustomObjectsApi", return_value=custom):
        first = asyncio.create_task(
            _as_caller("subject-alice", lambda: mod.k8s_sync_argocd_app("first"))
        )
        await started.wait()
        await _as_caller("subject-bob", lambda: mod.k8s_sync_argocd_app("second"))
        release.set()
        await first

    assert bodies["first"]["operation"]["initiatedBy"] == {"username": "subject-alice"}
    assert bodies["second"]["operation"]["initiatedBy"] == {"username": "subject-bob"}


_REQUESTER = "monolith.jomcgi.dev/requested-by"


@pytest.mark.asyncio
async def test_promotion_records_each_verified_subject(monkeypatch, caplog):
    mod = importlib.import_module("cluster.mcp")
    seen = {}
    monkeypatch.setattr(
        mod, "KubernetesClient", _promote_k8s(seen, _kargo_stage(), [_kargo_freight()])
    )
    caplog.set_level(logging.INFO, logger="cluster.mcp")

    created = []
    for subject, dry_run in (("subject-alice", True), ("subject-bob", False)):
        with _as(_principal(subject)):
            result = await mod.kargo_promote("embervm", "0.6.0", dry_run=dry_run)
        assert result["promotion"] == "prod.01abc.abc123"
        assert result["dry_run"] is dry_run
        created.append(seen["created"])

    (_, alice, alice_dry), (_, bob, bob_dry) = created
    assert (alice_dry, bob_dry) == (True, False)
    assert alice["metadata"] == {
        "generateName": "prod-",
        "namespace": "kargo-embervm",
        "annotations": {_REQUESTER: "subject-alice"},
    }
    assert bob["metadata"]["annotations"] == {_REQUESTER: "subject-bob"}
    assert alice["spec"] == bob["spec"]

    lines = [r.getMessage() for r in caplog.records if r.name == "cluster.mcp"]
    assert lines == [
        "kargo_promote: embervm 0.6.0 to kargo-embervm/prod requested by "
        "subject=subject-alice authority=standing (dry_run=True, rollback=False)",
        "kargo_promote: embervm 0.6.0 to kargo-embervm/prod requested by "
        "subject=subject-bob authority=standing (dry_run=False, rollback=False)",
    ]


@pytest.mark.asyncio
async def test_refused_or_denied_promotion_is_not_logged_as_requested(
    monkeypatch, caplog
):
    mod = importlib.import_module("cluster.mcp")
    seen = {}
    caplog.set_level(logging.INFO, logger="cluster.mcp")
    with _as(_principal("subject-alice")):
        monkeypatch.setattr(
            mod,
            "KubernetesClient",
            _promote_k8s(
                seen, _kargo_stage(current={"name": "prod.running"}), [_kargo_freight()]
            ),
        )
        refused = await mod.kargo_promote("embervm", "0.6.0")
        monkeypatch.setattr(
            mod,
            "KubernetesClient",
            _promote_k8s(seen, _kargo_stage(), [_kargo_freight()]),
        )
        seen["deny"] = True
        denied = await mod.kargo_promote("embervm", "0.6.0")
    assert refused["error"].startswith("not promoting:")
    assert denied["error"].startswith("promotion failed: HTTP 403")
    assert "created" not in seen
    assert not [r for r in caplog.records if "requested by" in r.getMessage()]


@pytest.mark.asyncio
async def test_anonymous_promotion_is_annotated_as_anonymous(monkeypatch, caplog):
    mod = importlib.import_module("cluster.mcp")
    seen = {}
    monkeypatch.setattr(
        mod, "KubernetesClient", _promote_k8s(seen, _kargo_stage(), [_kargo_freight()])
    )
    caplog.set_level(logging.INFO, logger="cluster.mcp")
    await mod.kargo_promote("embervm", "0.6.0", dry_run=True)
    assert seen["created"][1]["metadata"]["annotations"] == {_REQUESTER: "anonymous"}
    assert any(
        "subject=anonymous authority=anonymous" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_interleaved_promotions_keep_their_own_subjects(monkeypatch):
    mod = importlib.import_module("cluster.mcp")
    started, release = asyncio.Event(), asyncio.Event()
    bodies = {}

    class _K8s(_promote_k8s({}, _kargo_stage(), [_kargo_freight()])):
        async def create_kargo_promotion(self, namespace, body, dry_run=False):
            if dry_run:
                started.set()
                await release.wait()
            bodies[dry_run] = body
            return {"metadata": {"name": "prod.x"}}

    monkeypatch.setattr(mod, "KubernetesClient", _K8s)
    first = asyncio.create_task(
        _as_caller(
            "subject-alice", lambda: mod.kargo_promote("embervm", "0.6.0", dry_run=True)
        )
    )
    await started.wait()
    await _as_caller("subject-bob", lambda: mod.kargo_promote("embervm", "0.6.0"))
    release.set()
    await first
    assert bodies[True]["metadata"]["annotations"] == {_REQUESTER: "subject-alice"}
    assert bodies[False]["metadata"]["annotations"] == {_REQUESTER: "subject-bob"}


@pytest.mark.asyncio
async def test_mutation_schemas_offer_no_identity_override():
    importlib.import_module("cluster.mcp")
    from core.mcp_app import mcp

    tools = {t.name: t for t in await mcp.list_tools()}
    sync = tools["k8s_sync_argocd_app"].parameters
    promote = tools["kargo_promote"].parameters
    assert set(sync["properties"]) == {"name", "prune", "dry_run"}
    assert set(promote["properties"]) == {"app", "chart_version", "dry_run", "rollback"}


@pytest.mark.asyncio
async def test_policy_denies_non_operators_before_any_mutation(monkeypatch):
    """The unchanged group policy still stops a caller before the cluster call."""
    from fastmcp import Client
    from core.mcp_app import mcp

    mod = importlib.import_module("cluster.mcp")
    monkeypatch.delenv("MCP_GROUP_POLICY_ENFORCED", raising=False)
    patch_mock = AsyncMock(return_value={})
    custom, _ = _patching_k8s(mod, monkeypatch, patch_mock)
    seen = {}

    async def exercise(principal):
        with _as(principal):
            async with Client(mcp) as client:
                sync = await client.call_tool(
                    "k8s_sync_argocd_app", {"name": "my-app"}, raise_on_error=False
                )
        return sync

    with patch("cluster.kubernetes.client.CustomObjectsApi", return_value=custom):
        for principal in (_principal("subject-eve", groups=()), anonymous_principal()):
            assert (await exercise(principal)).is_error
        patch_mock.assert_not_awaited()

        monkeypatch.setattr(
            mod,
            "KubernetesClient",
            _promote_k8s(seen, _kargo_stage(), [_kargo_freight()]),
        )
        with _as(_principal("subject-eve", groups=())):
            async with Client(mcp) as client:
                denied = await client.call_tool(
                    "kargo_promote",
                    {"app": "embervm", "chart_version": "0.6.0"},
                    raise_on_error=False,
                )
        assert denied.is_error
        assert "created" not in seen

        # An operator passes the same gate and is attributed by subject.
        _patching_k8s(mod, monkeypatch, patch_mock)
        result = await exercise(_principal("subject-alice"))
        assert not result.is_error
    assert _bodies(patch_mock)[0]["operation"]["initiatedBy"] == {
        "username": "subject-alice"
    }
