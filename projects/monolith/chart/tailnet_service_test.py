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


def _api_ingress_policy(documents: list[dict]) -> dict:
    policies = [
        document
        for document in documents
        if document.get("kind") == "CiliumNetworkPolicy"
        and document.get("metadata", {}).get("name") == "kg-api-ingress"
    ]
    assert len(policies) == 1
    return policies[0]


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

    rules = _api_ingress_policy(documents)["spec"]["ingress"]
    proxy_rules = [
        rule
        for rule in rules
        if rule.get("fromEndpoints", [{}])[0]
        .get("matchLabels", {})
        .get("tailscale.com/parent-resource")
        == "kg-tailnet"
    ]
    assert len(proxy_rules) == 1
    assert proxy_rules[0]["fromEndpoints"][0]["matchLabels"] == {
        "k8s:io.kubernetes.pod.namespace": "tailscale",
        "tailscale.com/managed": "true",
        "tailscale.com/parent-resource-type": "svc",
        "tailscale.com/parent-resource": "kg-tailnet",
        "tailscale.com/parent-resource-ns": "default",
    }
    assert proxy_rules[0]["toPorts"] == [
        {"ports": [{"port": "8000", "protocol": "TCP"}]}
    ]
    assert _tailnet_network_policies(documents) == []


def test_gke_tailnet_service_does_not_render_cilium_policy():
    documents = _render(True, gke=True)
    assert len(_tailnet_services(documents)) == 1
    assert not any(
        document.get("kind") == "CiliumNetworkPolicy" for document in documents
    )
