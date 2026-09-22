"""Tests for the optional tailnet API Service."""

import os
import subprocess
from pathlib import Path

import yaml


def _render(
    tailnet_enabled: bool,
    *,
    gke: bool = False,
) -> list[dict]:
    chart_dir = Path(__file__).resolve().parent
    deploy_values = os.environ.get(
        "DEPLOY_VALUES", str(chart_dir.parent / "deploy" / "values.yaml")
    )
    command = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "kg",
        str(chart_dir),
        "-f",
        deploy_values,
    ]
    if gke:
        gke_values = os.environ.get(
            "GKE_VALUES", str(chart_dir.parent / "deploy" / "values-gke.yaml")
        )
        command.extend(["-f", gke_values])
    if tailnet_enabled:
        command.extend(["--set", "tailnet.enabled=true"])
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def _tailnet_services(documents: list[dict]) -> list[dict]:
    return [
        document
        for document in documents
        if document.get("kind") == "Service"
        and document.get("metadata", {}).get("name") == "kg-tailnet"
    ]


def _tailnet_network_policies(documents: list[dict]) -> list[dict]:
    return [
        document
        for document in documents
        if document.get("apiVersion") == "networking.k8s.io/v1"
        and document.get("kind") == "NetworkPolicy"
        and document.get("metadata", {}).get("name") == "kg-tailnet"
    ]


def test_tailnet_service_is_disabled_by_default():
    assert _tailnet_services(_render(False)) == []


def test_tailnet_service_exposes_only_api_port():
    documents = _render(True)
    services = _tailnet_services(documents)
    assert len(services) == 1
    service = services[0]
    assert service["metadata"]["annotations"] == {
        "tailscale.com/expose": "true",
        "tailscale.com/hostname": "monolith",
    }
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"] == [
        {"name": "api", "port": 80, "targetPort": "api", "protocol": "TCP"}
    ]
    assert service["spec"]["selector"]["app.kubernetes.io/component"] == "app"

    assert _tailnet_network_policies(documents) == []


def test_gke_tailnet_service_does_not_render_cilium_policy():
    documents = _render(True, gke=True)
    assert len(_tailnet_services(documents)) == 1
    assert not any(
        document.get("kind") == "CiliumNetworkPolicy" for document in documents
    )
