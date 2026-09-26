from __future__ import annotations

from dataclasses import replace

import pytest

from agent_kubernetes import mcp as subject
from agent_kubernetes.client import ObservationFailure, ReadResult
from auth.api import Authority, Principal, PrincipalKind, anonymous_principal


AUTHORIZED = Principal(
    subject="kg-agent-sa",
    actor=(),
    scope=("openid",),
    groups=("kg-agents",),
    email=None,
    kind=PrincipalKind.WORKLOAD,
    authority=Authority.STANDING,
)


class _Observer:
    instances = []
    read_result = ReadResult(
        items=[{"metadata": {"name": "monolith", "namespace": "argocd"}}],
        next_continue_token="next",
    )
    failure = None

    def __init__(self):
        self.read_request = None
        self.log_request = None
        self.closed = False
        self.__class__.instances.append(self)

    async def read(self, request):
        self.read_request = request
        if self.failure:
            raise self.failure
        return self.read_result

    async def get_unprojected(self, request):
        self.read_request = request
        if self.failure:
            raise self.failure
        return {
            "metadata": {"name": request.name},
            "spec": {"source": {"chart": "monolith", "targetRevision": "0.5.0"}},
            "status": {
                "sync": {"status": "Synced", "revision": "0.5.0"},
                "health": {"status": "Healthy"},
                "operationState": {
                    "phase": "Succeeded",
                    "syncResult": {"revision": "0.5.0"},
                },
            },
        }

    async def pod_logs(self, **kwargs):
        self.log_request = kwargs
        if self.failure:
            raise self.failure
        return {"logs": "line", "lines": 1, "truncated": False}

    async def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    _Observer.instances = []
    _Observer.failure = None
    monkeypatch.setattr(subject, "RestrictedKubernetesClient", _Observer)


@pytest.mark.parametrize(
    "principal",
    [
        anonymous_principal(),
        replace(AUTHORIZED, subject="another-service"),
        replace(AUTHORIZED, groups=()),
        replace(AUTHORIZED, kind=PrincipalKind.HUMAN, email="human@example.com"),
        replace(AUTHORIZED, authority=Authority.DELEGATED, actor=("operator",)),
    ],
)
@pytest.mark.asyncio
async def test_every_read_invocation_rejects_anonymous_or_wrong_principals(
    monkeypatch, principal
):
    monkeypatch.setattr(subject, "current_principal", lambda: principal)
    response = await subject.kubernetes_read(
        verb="list",
        api_group="core",
        resource="pods",
        namespace="monolith",
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "unauthorized"
    assert _Observer.instances == []


@pytest.mark.asyncio
async def test_every_log_invocation_rejects_wrong_principal(monkeypatch):
    monkeypatch.setattr(subject, "current_principal", anonymous_principal)
    response = await subject.kubernetes_pod_logs("monolith", "api-0")
    assert response["error"]["code"] == "unauthorized"
    assert _Observer.instances == []


@pytest.mark.asyncio
async def test_permitted_argocd_observation_reports_freshness_and_coverage(monkeypatch):
    monkeypatch.setattr(subject, "current_principal", lambda: AUTHORIZED)
    response = await subject.kubernetes_read(
        verb="list",
        api_group="argoproj.io",
        resource="applications",
        namespace="argocd",
        limit=10,
    )

    assert response["ok"] is True
    assert response["freshness"]["source"] == "kubernetes_api"
    assert response["freshness"]["successful"] is True
    assert response["coverage"] == {
        "verb": "list",
        "api_group": "argoproj.io",
        "api_version": "v1alpha1",
        "resource": "applications",
        "namespace": "argocd",
        "cluster_scoped": False,
        "page_limit": 10,
        "complete": False,
        "next_continue_token": "next",
    }
    assert _Observer.instances[0].read_request.rule.resource == "applications"
    assert _Observer.instances[0].closed is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"verb": "patch", "api_group": "argoproj.io", "resource": "applications"},
        {"verb": "list", "api_group": "core", "resource": "secrets"},
        {"verb": "list", "api_group": "core", "resource": "pods/exec"},
        {"verb": "list", "api_group": "core", "resource": "pods/proxy"},
        {"verb": "list", "api_group": "*", "resource": "pods"},
    ],
)
@pytest.mark.asyncio
async def test_authorized_principal_still_cannot_escape_allowlist(monkeypatch, kwargs):
    monkeypatch.setattr(subject, "current_principal", lambda: AUTHORIZED)
    response = await subject.kubernetes_read(**kwargs)
    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert _Observer.instances == []


@pytest.mark.asyncio
async def test_permitted_logs_report_bounds(monkeypatch):
    monkeypatch.setattr(subject, "current_principal", lambda: AUTHORIZED)
    response = await subject.kubernetes_pod_logs(
        "embervm",
        "ember-api-0",
        container="api",
        tail_lines=20,
        since_seconds=300,
        previous=True,
    )
    assert response["ok"] is True
    assert response["coverage"] == {
        "api_group": "core",
        "api_version": "v1",
        "resource": "pods/log",
        "verb": "get",
        "namespace": "embervm",
        "pod": "ember-api-0",
        "container": "api",
        "tail_lines": 20,
        "since_seconds": 300,
        "previous": True,
        "max_bytes": 32_000,
    }
    assert _Observer.instances[0].closed is True


@pytest.mark.asyncio
async def test_observation_failure_is_explicit_and_client_is_closed(monkeypatch):
    monkeypatch.setattr(subject, "current_principal", lambda: AUTHORIZED)
    _Observer.failure = ObservationFailure(
        "timeout", "Kubernetes observation timed out"
    )
    response = await subject.kubernetes_read(
        verb="list",
        api_group="kargo.akuity.io",
        resource="freights",
        namespace="kargo-monolith",
    )
    assert response["ok"] is False
    assert response["error"] == {
        "code": "timeout",
        "message": "Kubernetes observation timed out",
    }
    assert response["freshness"]["successful"] is False
    assert response["coverage"]["resource"] == "freights"
    assert response["coverage"]["complete"] is False
    assert _Observer.instances[0].closed is True


@pytest.mark.parametrize(
    "principal",
    [
        anonymous_principal(),
        replace(AUTHORIZED, subject="another-service"),
        replace(AUTHORIZED, groups=()),
    ],
)
@pytest.mark.asyncio
async def test_verify_deployment_rejects_wrong_principals(monkeypatch, principal):
    monkeypatch.setattr(subject, "current_principal", lambda: principal)
    response = await subject.verify_deployment("monolith")
    assert response["ok"] is False
    assert response["error"]["code"] == "unauthorized"
    assert _Observer.instances == []


@pytest.mark.asyncio
async def test_verify_deployment_reads_only_the_argocd_application(monkeypatch):
    monkeypatch.setattr(subject, "current_principal", lambda: AUTHORIZED)
    response = await subject.verify_deployment("monolith", expected_revision="0.4.9")
    assert response["ok"] is True
    assert response["verdict"] == "verified"
    assert response["live_revision"] == "0.5.0"
    assert response["freshness"]["successful"] is True
    request = _Observer.instances[0].read_request
    assert (request.verb, request.rule.api_group, request.rule.resource) == (
        "get",
        "argoproj.io",
        "applications",
    )
    assert (request.namespace, request.name) == ("argocd", "monolith")
    assert _Observer.instances[0].closed is True


@pytest.mark.asyncio
async def test_verify_deployment_reports_not_yet_reached(monkeypatch):
    monkeypatch.setattr(subject, "current_principal", lambda: AUTHORIZED)
    response = await subject.verify_deployment("monolith", expected_revision="0.5.1")
    assert response["ok"] is True
    assert response["verdict"] == "in_progress"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"app": "Not_A_Name"},
        {"app": "monolith", "expected_revision": ""},
        {"app": "monolith", "expected_revision": "x" * 65},
    ],
)
@pytest.mark.asyncio
async def test_verify_deployment_rejects_bad_input_before_io(monkeypatch, kwargs):
    monkeypatch.setattr(subject, "current_principal", lambda: AUTHORIZED)
    response = await subject.verify_deployment(**kwargs)
    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert _Observer.instances == []


@pytest.mark.asyncio
async def test_verify_deployment_failure_is_explicit_and_client_is_closed(monkeypatch):
    monkeypatch.setattr(subject, "current_principal", lambda: AUTHORIZED)
    _Observer.failure = ObservationFailure("not_found", "resource was not found")
    response = await subject.verify_deployment("missing-app")
    assert response["ok"] is False
    assert response["error"]["code"] == "not_found"
    assert _Observer.instances[0].closed is True


@pytest.mark.asyncio
async def test_verify_deployment_rejects_a_sha_for_a_chart_app(monkeypatch):
    monkeypatch.setattr(subject, "current_principal", lambda: AUTHORIZED)
    response = await subject.verify_deployment("monolith", expected_revision="abcdef1")
    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    assert _Observer.instances[0].closed is True
