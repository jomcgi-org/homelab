"""Private backup advisory activation and reuse of the ConfigMap read grant."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent


def render(enabled=None, extra=()):
    result = subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "monolith",
            str(CHART),
            "--namespace",
            "monolith",
            "--set",
            "jobs.image.repository=registry.invalid/jobs",
            "--set-string",
            "jobs.image.digest=sha256:test",
            *(
                []
                if enabled is None
                else [
                    "--set",
                    f"healthAlerts.cnpgBackups.enabled={str(enabled).lower()}",
                ]
            ),
            *extra,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def backend_env(docs):
    deployment = next(
        d
        for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "monolith"
    )
    backend = next(
        c
        for c in deployment["spec"]["template"]["spec"]["containers"]
        if c["name"] == "backend"
    )
    assert backend["livenessProbe"]["httpGet"]["path"] == "/healthz"
    return {e["name"]: e.get("value") for e in backend["env"]}


def test_reader_defaults_off_outside_hub():
    for path in (
        CHART / "values.yaml",
        CHART.parent / "deploy/values.yaml",
    ):
        assert (
            yaml.safe_load(path.read_text())["healthAlerts"]["cnpgBackups"]["enabled"]
            is False
        )
    env = backend_env(render())
    assert env["CNPG_BACKUP_HEALTH_ENABLED"] == "false"
    assert "CNPG_BACKUP_STATUS_CONFIGMAP" not in env


def test_hub_reader_is_enabled_with_the_required_backup_targets():
    env = backend_env(
        render(
            extra=[
                "--values",
                str(CHART.parent / "deploy/values.yaml"),
                "--values",
                str(CHART.parent / "deploy/values-gke.yaml"),
            ]
        )
    )
    assert env["CNPG_BACKUP_HEALTH_ENABLED"] == "true"
    assert env["CNPG_BACKUP_STATUS_NAMESPACE"] == "otel-collector"
    assert env["CNPG_BACKUP_STATUS_CONFIGMAP"] == "cnpg-backup-status"
    assert json.loads(env["CNPG_BACKUP_TARGETS"]) == [
        {"namespace": "monolith", "cluster": "monolith-pg"},
        {"namespace": "authentik", "cluster": "authentik-pg"},
        {"namespace": "mcp", "cluster": "context-forge-pg"},
    ]
    assert json.loads(env["CNPG_BACKUP_OPTIONAL_TARGETS"]) == [
        {"namespace": "loom", "cluster": "loom-pg"}
    ]


def test_reader_enablement_wires_targets_and_reuses_existing_read_permission():
    off, on = render(), render(True)
    env = backend_env(on)
    assert env["CNPG_BACKUP_HEALTH_ENABLED"] == "true"
    assert env["CNPG_BACKUP_STATUS_NAMESPACE"] == "otel-collector"
    assert env["CNPG_BACKUP_STATUS_CONFIGMAP"] == "cnpg-backup-status"
    assert json.loads(env["CNPG_BACKUP_TARGETS"]) == [
        {"namespace": "monolith", "cluster": "monolith-pg"},
        {"namespace": "authentik", "cluster": "authentik-pg"},
        {"namespace": "mcp", "cluster": "context-forge-pg"},
    ]
    assert json.loads(env["CNPG_BACKUP_OPTIONAL_TARGETS"]) == [
        {"namespace": "loom", "cluster": "loom-pg"}
    ]
    kinds = {
        "ClusterRole",
        "Role",
        "ClusterRoleBinding",
        "RoleBinding",
        "HTTPRoute",
        "Service",
    }
    assert [d for d in off if d["kind"] in kinds] == [
        d for d in on if d["kind"] in kinds
    ]
    role = next(
        d
        for d in on
        if d["kind"] == "ClusterRole" and d["metadata"]["name"] == "monolith"
    )
    assert any(
        "configmaps" in r["resources"] and "get" in r["verbs"] for r in role["rules"]
    )
    assert not any(
        "clusters" in r["resources"] and "postgresql.cnpg.io" in r["apiGroups"]
        for r in role["rules"]
    )


def test_reader_cannot_enable_without_existing_cluster_read_access():
    with pytest.raises(
        AssertionError, match="healthAlerts.cnpgBackups requires rbac.clusterAccess"
    ):
        render(True, ["--set", "rbac.clusterAccess.enabled=false"])


def test_reader_handoff_location_can_be_configured():
    env = backend_env(
        render(
            True,
            [
                "--set",
                "healthAlerts.cnpgBackups.namespace=observability",
                "--set",
                "healthAlerts.cnpgBackups.configMapName=backup-observations",
            ],
        )
    )
    assert env["CNPG_BACKUP_STATUS_NAMESPACE"] == "observability"
    assert env["CNPG_BACKUP_STATUS_CONFIGMAP"] == "backup-observations"
