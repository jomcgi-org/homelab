"""Regression tests for the private monolith writable filesystem boundary."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

_CONTAINER_TMP_VOLUMES = {
    "backend": "backend-tmp",
    "progress-ingest": "progress-ingest-tmp",
    "frontend": "frontend-tmp",
}


def _render(*values_files: Path, set_values: tuple[str, ...] = ()) -> list[dict]:
    chart_dir = Path(__file__).resolve().parent
    command = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "monolith",
        str(chart_dir),
        "--namespace",
        "monolith",
    ]
    for values_file in values_files:
        command.extend(["--values", str(values_file)])
    for set_value in set_values:
        command.extend(["--set", set_value])
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def _values_path(env_name: str, fallback: Path) -> Path:
    return Path(os.environ.get(env_name, fallback))


def _deployment(documents: list[dict], name: str = "monolith") -> dict:
    deployments = [
        document
        for document in documents
        if document.get("kind") == "Deployment"
        and document.get("metadata", {}).get("name") == name
    ]
    assert len(deployments) == 1
    return deployments[0]


@pytest.fixture(params=("chart-default", "production", "production-gke", "development"))
def app_pod_spec(request: pytest.FixtureRequest) -> dict:
    chart_dir = Path(__file__).resolve().parent
    deploy_values = _values_path(
        "DEPLOY_VALUES", chart_dir.parent / "deploy" / "values.yaml"
    )
    dev_values = _values_path(
        "DEV_VALUES", chart_dir.parent / "dev" / "deploy" / "values.yaml"
    )
    gke_values = _values_path(
        "GKE_VALUES", chart_dir.parent / "deploy" / "values-gke.yaml"
    )
    values_by_environment = {
        "chart-default": (),
        "production": (deploy_values,),
        "production-gke": (deploy_values, gke_values),
        "development": (deploy_values, dev_values),
    }
    deployment = _deployment(_render(*values_by_environment[request.param]))
    return deployment["spec"]["template"]["spec"]


@pytest.fixture
def whatsapp_pod_spec() -> dict:
    deployment = _deployment(
        _render(set_values=("whatsapp.enabled=true",)),
        "monolith-whatsapp",
    )
    return deployment["spec"]["template"]["spec"]


def test_private_containers_have_read_only_roots(app_pod_spec: dict) -> None:
    containers = {
        container["name"]: container for container in app_pod_spec["containers"]
    }
    assert set(containers) == set(_CONTAINER_TMP_VOLUMES)

    for container in containers.values():
        security_context = container["securityContext"]
        assert security_context["readOnlyRootFilesystem"] is True
        assert security_context["runAsNonRoot"] is True
        assert security_context["runAsUser"] == 65532
        assert security_context["allowPrivilegeEscalation"] is False
        assert security_context["capabilities"]["drop"] == ["ALL"]


def test_each_container_has_only_its_own_writable_tmp(app_pod_spec: dict) -> None:
    containers = {
        container["name"]: container for container in app_pod_spec["containers"]
    }
    for container_name, volume_name in _CONTAINER_TMP_VOLUMES.items():
        assert containers[container_name]["volumeMounts"] == [
            {"name": volume_name, "mountPath": "/tmp"}
        ]

    assert app_pod_spec["volumes"] == [
        {"name": volume_name, "emptyDir": {}}
        for volume_name in _CONTAINER_TMP_VOLUMES.values()
    ]


def test_tmp_volumes_are_writable_by_the_non_root_process(app_pod_spec: dict) -> None:
    assert app_pod_spec["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 65532,
        "runAsGroup": 65532,
        "fsGroup": 65532,
        "seccompProfile": {"type": "RuntimeDefault"},
    }


def test_whatsapp_gateway_has_only_writable_tmp(whatsapp_pod_spec: dict) -> None:
    assert len(whatsapp_pod_spec["containers"]) == 1
    container = whatsapp_pod_spec["containers"][0]
    assert container["name"] == "whatsapp"
    assert container["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 65532,
        "readOnlyRootFilesystem": True,
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
    }
    assert container["volumeMounts"] == [{"name": "whatsapp-tmp", "mountPath": "/tmp"}]
    assert whatsapp_pod_spec["volumes"] == [{"name": "whatsapp-tmp", "emptyDir": {}}]
    assert whatsapp_pod_spec["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 65532,
        "runAsGroup": 65532,
        "fsGroup": 65532,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
