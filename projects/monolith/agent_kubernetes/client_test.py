from __future__ import annotations

import pytest

from agent_kubernetes import client as subject


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"verb": "delete"}, "verb"),
        ({"verb": "watch"}, "verb"),
        ({"api_group": "*"}, "wildcards"),
        ({"resource": "*"}, "wildcards"),
        ({"resource": "secrets"}, "not allowed"),
        ({"resource": "pods/exec"}, "not allowed"),
        ({"resource": "pods/attach"}, "not allowed"),
        ({"resource": "pods/portforward"}, "not allowed"),
        ({"resource": "pods/proxy"}, "not allowed"),
        (
            {"api_group": "rbac.authorization.k8s.io", "resource": "roles"},
            "not allowed",
        ),
        (
            {
                "api_group": "rbac.authorization.k8s.io",
                "resource": "clusterrolebindings",
            },
            "not allowed",
        ),
        ({"subresource": "exec"}, "subresources"),
        ({"namespace": "default"}, "namespace is not allowed"),
        ({"namespace": "*"}, "DNS name"),
        ({"limit": 0}, "limit"),
        ({"limit": 101}, "limit"),
        ({"continue_token": "not a token!"}, "malformed"),
    ],
)
def test_read_validation_rejects_unsafe_or_malformed_inputs(overrides, message):
    request = {
        "verb": "list",
        "api_group": "core",
        "resource": "pods",
        "namespace": "monolith",
        "limit": 50,
    }
    request.update(overrides)
    with pytest.raises(subject.InvalidObservationRequest, match=message):
        subject.validate_read_request(**request)


def test_cluster_scope_is_explicit_and_namespaced_scope_is_required():
    nodes = subject.validate_read_request(
        verb="list", api_group="core", resource="nodes"
    )
    assert nodes.rule.namespaced is False
    assert nodes.namespace is None

    with pytest.raises(subject.InvalidObservationRequest, match="must be omitted"):
        subject.validate_read_request(
            verb="list",
            api_group="core",
            resource="nodes",
            namespace="monolith",
        )
    with pytest.raises(subject.InvalidObservationRequest, match="DNS name"):
        subject.validate_read_request(verb="list", api_group="core", resource="pods")


def test_argocd_and_kargo_are_limited_to_recorded_namespaces():
    app = subject.validate_read_request(
        verb="get",
        api_group="argoproj.io",
        resource="applications",
        namespace="argocd",
        name="monolith",
    )
    freight = subject.validate_read_request(
        verb="list",
        api_group="kargo.akuity.io",
        resource="freights",
        namespace="kargo-embervm",
    )
    assert app.rule.version == "v1alpha1"
    assert freight.namespace == "kargo-embervm"

    with pytest.raises(subject.InvalidObservationRequest, match="not allowed"):
        subject.validate_read_request(
            verb="list",
            api_group="argoproj.io",
            resource="applications",
            namespace="monolith",
        )
    with pytest.raises(subject.InvalidObservationRequest, match="not allowed"):
        subject.validate_read_request(
            verb="list",
            api_group="kargo.akuity.io",
            resource="freights",
            namespace="kargo",
        )


class _FakeApiClient:
    def __init__(self):
        self.closed = False

    @staticmethod
    def sanitize_for_serialization(value):
        return value

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_typed_list_constructs_one_bounded_paginated_request(monkeypatch):
    calls = []

    class Core:
        async def list_namespaced_pod(self, namespace, **kwargs):
            calls.append((namespace, kwargs))
            return {
                "items": [
                    {
                        "metadata": {"name": "api-1", "namespace": namespace},
                        "status": {"phase": "Running"},
                    }
                ],
                "metadata": {"continue": "next-page"},
            }

    monkeypatch.setattr(subject.client, "CoreV1Api", lambda api: Core())
    observer = subject.RestrictedKubernetesClient()
    observer._api = _FakeApiClient()
    request = subject.validate_read_request(
        verb="list",
        api_group="core",
        resource="pods",
        namespace="monolith",
        limit=12,
        continue_token="page-1",
    )

    result = await observer.read(request)

    assert calls == [
        (
            "monolith",
            {
                "limit": 12,
                "_request_timeout": subject.REQUEST_TIMEOUT_SECONDS,
                "_continue": "page-1",
            },
        )
    ]
    assert result.next_continue_token == "next-page"
    assert result.items == [
        {
            "metadata": {"name": "api-1", "namespace": "monolith"},
            "status": {"phase": "Running"},
        }
    ]


@pytest.mark.asyncio
async def test_custom_get_constructs_exact_argocd_request(monkeypatch):
    calls = []

    class Custom:
        async def get_namespaced_custom_object(self, **kwargs):
            calls.append(kwargs)
            return {
                "apiVersion": "argoproj.io/v1alpha1",
                "kind": "Application",
                "metadata": {"name": "monolith", "namespace": "argocd"},
                "spec": {"destination": {"namespace": "monolith"}},
                "status": {"sync": {"status": "Synced"}},
            }

    monkeypatch.setattr(subject.client, "CustomObjectsApi", lambda api: Custom())
    observer = subject.RestrictedKubernetesClient()
    observer._api = _FakeApiClient()
    request = subject.validate_read_request(
        verb="get",
        api_group="argoproj.io",
        resource="applications",
        namespace="argocd",
        name="monolith",
    )

    result = await observer.read(request)

    assert calls == [
        {
            "group": "argoproj.io",
            "version": "v1alpha1",
            "plural": "applications",
            "_request_timeout": subject.REQUEST_TIMEOUT_SECONDS,
            "namespace": "argocd",
            "name": "monolith",
        }
    ]
    assert result.items[0]["status"] == {"sync": {"status": "Synced"}}


@pytest.mark.asyncio
async def test_pod_get_prunes_environment_values_from_the_observation(monkeypatch):
    class Core:
        async def read_namespaced_pod(self, name, namespace, **kwargs):
            return {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": name, "namespace": namespace},
                "spec": {
                    "serviceAccountName": "monolith",
                    "containers": [
                        {
                            "name": "api",
                            "image": "example/api:v1",
                            "env": [{"name": "INLINE_TOKEN", "value": "do-not-return"}],
                        }
                    ],
                },
                "status": {"phase": "Running"},
            }

    monkeypatch.setattr(subject.client, "CoreV1Api", lambda api: Core())
    observer = subject.RestrictedKubernetesClient()
    observer._api = _FakeApiClient()
    request = subject.validate_read_request(
        verb="get",
        api_group="core",
        resource="pods",
        namespace="monolith",
        name="api-0",
    )

    result = await observer.read(request)

    assert result.items[0]["spec"] == {
        "serviceAccountName": "monolith",
        "containers": [{"name": "api", "image": "example/api:v1"}],
    }
    assert "do-not-return" not in str(result.items[0])


@pytest.mark.asyncio
async def test_kargo_list_constructs_exact_namespaced_request(monkeypatch):
    calls = []

    class Custom:
        async def list_namespaced_custom_object(self, **kwargs):
            calls.append(kwargs)
            return {
                "items": [
                    {
                        "metadata": {
                            "name": "monolith-0.80.0",
                            "namespace": "kargo-monolith",
                        },
                        "charts": [
                            {
                                "repoURL": "oci://example/charts/monolith",
                                "version": "0.80.0",
                            }
                        ],
                    }
                ],
                "metadata": {},
            }

    monkeypatch.setattr(subject.client, "CustomObjectsApi", lambda api: Custom())
    observer = subject.RestrictedKubernetesClient()
    observer._api = _FakeApiClient()
    request = subject.validate_read_request(
        verb="list",
        api_group="kargo.akuity.io",
        resource="freights",
        namespace="kargo-monolith",
        limit=25,
    )

    result = await observer.read(request)

    assert calls == [
        {
            "group": "kargo.akuity.io",
            "version": "v1alpha1",
            "plural": "freights",
            "_request_timeout": subject.REQUEST_TIMEOUT_SECONDS,
            "limit": 25,
            "namespace": "kargo-monolith",
        }
    ]
    assert result.items[0]["charts"][0]["version"] == "0.80.0"


@pytest.mark.asyncio
async def test_events_are_permitted_and_projected_without_raw_manifests(monkeypatch):
    class Core:
        async def list_namespaced_event(self, namespace, **kwargs):
            return {
                "items": [
                    {
                        "metadata": {"name": "failed-1", "namespace": namespace},
                        "involvedObject": {"kind": "Pod", "name": "api-0"},
                        "type": "Warning",
                        "reason": "Failed",
                        "message": "container failed",
                        "count": 2,
                    }
                ],
                "metadata": {},
            }

    monkeypatch.setattr(subject.client, "CoreV1Api", lambda api: Core())
    observer = subject.RestrictedKubernetesClient()
    observer._api = _FakeApiClient()
    request = subject.validate_read_request(
        verb="list",
        api_group="core",
        resource="events",
        namespace="monolith-agents",
    )

    result = await observer.read(request)

    assert result.items == [
        {
            "metadata": {"name": "failed-1", "namespace": "monolith-agents"},
            "involvedObject": {"kind": "Pod", "name": "api-0"},
            "type": "Warning",
            "reason": "Failed",
            "message": "container failed",
            "count": 2,
        }
    ]


@pytest.mark.asyncio
async def test_logs_construct_bounded_request_and_cap_returned_bytes(monkeypatch):
    calls = []

    class Core:
        async def read_namespaced_pod_log(self, **kwargs):
            calls.append(kwargs)
            return "x" * (subject.LOG_BYTES_MAX + 100)

    monkeypatch.setattr(subject.client, "CoreV1Api", lambda api: Core())
    observer = subject.RestrictedKubernetesClient()
    observer._api = _FakeApiClient()

    result = await observer.pod_logs(
        namespace="embervm",
        pod="ember-api-0",
        container="api",
        tail_lines=25,
        since_seconds=120,
        previous=True,
    )

    assert calls == [
        {
            "name": "ember-api-0",
            "namespace": "embervm",
            "container": "api",
            "tail_lines": 25,
            "since_seconds": 120,
            "previous": True,
            "timestamps": True,
            "_request_timeout": subject.REQUEST_TIMEOUT_SECONDS,
        }
    ]
    assert len(result["logs"].encode()) == subject.LOG_BYTES_MAX
    assert result["truncated"] is True


def test_log_validation_rejects_unbounded_and_unsafe_requests():
    with pytest.raises(subject.InvalidObservationRequest, match="not allowed"):
        subject.validate_log_request(
            namespace="kube-system",
            pod="api",
            container=None,
            tail_lines=200,
            since_seconds=60,
            previous=False,
        )
    with pytest.raises(subject.InvalidObservationRequest, match="tail_lines"):
        subject.validate_log_request(
            namespace="monolith",
            pod="api",
            container=None,
            tail_lines=501,
            since_seconds=60,
            previous=False,
        )
    with pytest.raises(subject.InvalidObservationRequest, match="since_seconds"):
        subject.validate_log_request(
            namespace="monolith",
            pod="api",
            container=None,
            tail_lines=200,
            since_seconds=86401,
            previous=False,
        )


@pytest.mark.asyncio
async def test_api_failures_are_classified_without_response_details(monkeypatch):
    class Forbidden(Exception):
        status = 403

    class Core:
        async def list_namespaced_event(self, namespace, **kwargs):
            raise Forbidden("credential-bearing upstream detail")

    monkeypatch.setattr(subject.client, "CoreV1Api", lambda api: Core())
    observer = subject.RestrictedKubernetesClient()
    observer._api = _FakeApiClient()
    request = subject.validate_read_request(
        verb="list",
        api_group="core",
        resource="events",
        namespace="monolith-agents",
    )

    with pytest.raises(subject.ObservationFailure) as raised:
        await observer.read(request)
    assert raised.value.code == "forbidden"
    assert "credential" not in raised.value.message
