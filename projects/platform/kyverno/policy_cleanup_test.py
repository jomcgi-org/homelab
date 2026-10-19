"""Render the #7072 policy cleanup and preserve its workflow consumers.

Expected clone rules and aggregation roles are the unchanged semantic bodies
from b2f623c9afe6714d83ba91569348084307e95dbe. No cluster or Secret reads occur.
HELM_BIN is supplied by Bazel; local pytest uses helm from PATH.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
POLICY = "clone-monolith-workflows-secrets-v2"
MARKER = "otel.injected-by"
RETAINED = [
    ("clone-to-monolith-workflows", "monolith", "monolith-pg-app"),
    ("clone-secrets-to-monolith-workflows", "monolith", "monolith-secrets"),
    ("clone-chat-secrets-to-monolith-workflows", "monolith", "monolith-chat-secrets"),
    ("clone-openrouter-to-monolith-workflows", "monolith", "goosecracker"),
    ("clone-meta-spark-to-monolith-workflows", "monolith", "monolith-meta-spark"),
    ("clone-dev-pg-app-to-monolith-workflows", "monolith-dev", "monolith-dev-pg-app"),
    ("clone-r2-s3-to-monolith-workflows", "monolith", "monolith-r2-s3"),
]


def _render(chart, *, values=("values.yaml",), extra=()):
    monolith = chart == "monolith"
    path = ROOT / "projects" / ("monolith/chart" if monolith else f"platform/{chart}")
    namespace = (
        "monolith"
        if monolith
        else ("kyverno" if chart == "kyverno" else "monolith-workflows")
    )
    command = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        chart,
        str(path),
        "--namespace",
        namespace,
    ]
    for name in values:
        command.extend(["--values", str(path / name)])
    command.extend(extra)
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _kind(docs, kind):
    return {doc["metadata"]["name"]: doc for doc in docs if doc["kind"] == kind}


def _assert_no_marker(value):
    if isinstance(value, dict):
        assert MARKER not in value
        for child in value.values():
            _assert_no_marker(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_marker(child)


def test_hub_policies_preserve_exact_retained_rule_bodies():
    # Hub Application uses only values.yaml for Kyverno.
    policies = _kind(_render("kyverno"), "ClusterPolicy")
    assert set(policies) == {POLICY, "require-resource-requests"}
    expected = []
    for rule, namespace, secret in RETAINED:
        expected.append(
            {
                "name": rule,
                "skipBackgroundRequests": True,
                "match": {
                    "any": [
                        {
                            "resources": {
                                "kinds": ["Secret"],
                                "namespaces": [namespace],
                                "names": [secret],
                            }
                        }
                    ]
                },
                "generate": {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "name": secret,
                    "namespace": "monolith-workflows",
                    "synchronize": True,
                    "generateExisting": True,
                    "clone": {"namespace": namespace, "name": secret},
                },
            }
        )
    # Full spec equality also excludes mutation/validation, extra rules and gates.
    assert policies[POLICY]["spec"] == {"background": True, "rules": expected}
    audit = policies["require-resource-requests"]["spec"]
    assert audit["validationFailureAction"] == "Audit"
    assert audit["background"] is True
    (rule,) = audit["rules"]
    assert rule["skipBackgroundRequests"] is True
    assert rule["match"] == {
        "any": [
            {
                "resources": {
                    "kinds": ["Deployment", "StatefulSet", "DaemonSet"],
                    "namespaces": ["monolith", "monolith-public"],
                }
            }
        ]
    }
    assert rule["validate"]["allowExistingViolations"] is True
    assert rule["validate"]["pattern"] == {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "resources": {
                                "requests": {"cpu": "?*", "memory": "?*"},
                                "limits": {"memory": "?*"},
                            }
                        }
                    ],
                }
            }
        }
    }


def test_aggregation_roles_are_unchanged():
    roles = _kind(_render("kyverno"), "ClusterRole")
    for controller, verbs in (
        ("background", ["get", "list", "watch", "create", "update", "delete"]),
        ("admission", ["get", "list", "watch"]),
    ):
        name = f"kyverno:clone-monolith-pg-secret-{controller}"
        assert roles[name] == {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {
                "name": name,
                "labels": {
                    "app.kubernetes.io/part-of": "kyverno",
                    f"rbac.kyverno.io/aggregate-to-{controller}-controller": "true",
                },
            },
            "rules": [{"apiGroups": [""], "resources": ["secrets"], "verbs": verbs}],
        }


def test_injection_is_removed_even_with_legacy_enable_override():
    chart = ROOT / "projects/platform/kyverno"
    assert not (chart / "templates/otel-injection-policy.yaml").exists()
    assert "otelInjection" not in yaml.safe_load((chart / "values.yaml").read_text())
    docs = _render("kyverno", extra=("--set", "otelInjection.enabled=true"))
    assert set(_kind(docs, "ClusterPolicy")) == {POLICY, "require-resource-requests"}
    assert "otel-config" not in _kind(docs, "ConfigMap")
    _assert_no_marker(docs)


def test_resource_audit_remains_independently_optional():
    docs = _render("kyverno", extra=("--set", "requireResources.enabled=false"))
    assert set(_kind(docs, "ClusterPolicy")) == {POLICY}


@pytest.mark.parametrize(
    "values", [("values.yaml",), ("values.yaml", "values-gke.yaml")]
)
@pytest.mark.parametrize("server_enabled", [True, False])
def test_argo_annotations_preserve_metrics_and_disabled_server(values, server_enabled):
    docs = _render(
        "argo-workflows",
        values=values,
        extra=(
            "--set",
            f"argo-workflows.server.enabled={str(server_enabled).lower()}",
        ),
    )
    deployments = _kind(docs, "Deployment")
    expected = {"argo-workflows-workflow-controller"}
    if server_enabled:
        expected.add("argo-workflows-server")
    assert set(deployments) == expected
    _assert_no_marker(docs)
    for deployment in deployments.values():
        annotations = deployment["spec"]["template"]["metadata"]["annotations"]
        assert annotations["prometheus.io/scrape"] == "true"
        assert annotations["prometheus.io/port"] == "9090"
        assert annotations["prometheus.io/path"] == "/metrics"


@pytest.mark.parametrize(
    "overlay,suspended",
    [
        (None, False),
        ("values-gke.yaml", True),
        ("values-home-suspend.yaml", True),
    ],
)
def test_renovate_annotations_preserve_suspension_and_secret_access(overlay, suspended):
    values = ("values.yaml", overlay) if overlay else ("values.yaml",)
    docs = _render("renovate", values=values)
    workflows = _kind(docs, "CronWorkflow")
    assert set(workflows) == {"renovate", "renovate-apko-lock-maintenance"}
    _assert_no_marker(docs)
    for workflow in workflows.values():
        assert workflow["spec"]["suspend"] is suspended
        spec = workflow["spec"]["workflowSpec"]
        assert spec["podMetadata"]["annotations"] == {}
        assert spec["serviceAccountName"] == "argo-workflow"
        assert spec["securityContext"]["runAsNonRoot"] is True
        for template in spec["templates"]:
            container = template["container"]
            token = next(
                env for env in container["env"] if env["name"] == "RENOVATE_TOKEN"
            )
            assert token["valueFrom"]["secretKeyRef"] == {
                "name": "renovate-github",
                "key": "RENOVATE_TOKEN",
            }
            assert container["resources"]["requests"]
            assert container["securityContext"]["readOnlyRootFilesystem"] is True


@pytest.mark.parametrize("with_image", [False, True])
def test_monolith_job_gate_preserves_explicit_telemetry(with_image):
    # Synthetic image and endpoint only exercise conditional rendering locally.
    # Checked-in activation, collector settings and exporter values are untouched.
    extra = ["--set", "backend.otelEndpoint=http://collector.example.test/v1/traces"]
    if with_image:
        extra += [
            "--set",
            "jobs.image.repository=example.test/jobs",
            "--set-string",
            "jobs.image.digest=sha256:" + "0" * 64,
        ]
    workflows = _kind(_render("monolith", extra=extra), "CronWorkflow")
    assert bool(workflows) is with_image
    _assert_no_marker(list(workflows.values()))
    for workflow in workflows.values():
        for template in workflow["spec"]["workflowSpec"]["templates"]:
            env = {entry["name"]: entry for entry in template["container"]["env"]}
            assert env["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"]["value"] == (
                "http://collector.example.test/v1/traces"
            )
            assert env["DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
                "name": "monolith-pg-app",
                "key": "uri",
            }
