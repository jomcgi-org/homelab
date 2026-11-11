"""Render the hub to guard the status-replay repair's compatibility boundary."""

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.fixture(scope="module")
def resources():
    chart = Path(__file__).resolve().parent
    result = subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "argocd",
            str(chart),
            "--namespace",
            "argocd",
            "-f",
            str(chart / "values.yaml"),
            "-f",
            str(chart / "values-gke.yaml"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in yaml.safe_load_all(result.stdout)
        if doc
    }


def test_every_argo_component_uses_fixed_vendor_image(resources):
    images = {
        c["image"]
        for doc in resources.values()
        for field in ("containers", "initContainers")
        for c in doc.get("spec", {}).get("template", {}).get("spec", {}).get(field, [])
    }
    argo = {image for image in images if "argoproj/argocd:" in image}
    assert len(argo) == 1
    version = tuple(map(int, re.search(r":v(\d+)\.(\d+)\.(\d+)$", argo.pop()).groups()))
    # This supported patch contains upstream #28201; don't fall back to the
    # chart's older appVersion or upgrade major/minor without migration review.
    assert version[:2] == (3, 3) and version[2] >= 18
    assert "ecr-public.aws.com/docker/library/redis:7.2.11-alpine" in images
    assert "ghcr.io/dexidp/dex:v2.44.0" in images


def test_hub_auth_and_reconciliation_defaults_survive_chart_upgrade(resources):
    params = resources["ConfigMap", "argocd-cmd-params-cm"]["data"]
    expected = {
        "server.disable.auth": "false",
        "server.insecure": "false",
        "server.enable.proxy.extension": "false",
        "server.rootpath": "",
        "server.basehref": "/",
        "application.namespaces": "",
        "applicationsetcontroller.namespaces": "",
        "applicationsetcontroller.policy": "sync",
        "hydrator.enabled": "false",
        "reposerver.repo.cache.expiration": "10m",
        "controller.operation.processors": "10",
        "controller.status.processors": "20",
    }
    assert expected.items() <= params.items()
    cm = resources["ConfigMap", "argocd-cm"]["data"]
    assert cm["admin.enabled"] == "true"
    assert cm["accounts.mcp-agent"] == "apiKey"
    assert cm["timeout.reconciliation"] == "180s"
    assert cm["timeout.reconciliation.jitter"] == "0s"
    assert not cm.get("oidc.config") and not cm.get("dex.config")
    assert resources["ConfigMap", "argocd-rbac-cm"]["data"][
        "policy.csv"
    ].splitlines() == [
        "p, mcp-agent, applications, *, */*, allow",
        "p, mcp-agent, projects, get, *, allow",
        "p, mcp-agent, clusters, get, *, allow",
    ]


def test_upgrade_does_not_expose_ui_or_change_webhook(resources):
    routes = [doc for (kind, _), doc in resources.items() if kind == "HTTPRoute"]
    assert len(routes) == 1
    assert routes[0]["metadata"]["name"] == "argocd-github-webhook"
    assert (
        routes[0]["spec"]["rules"][0]["matches"][0]["path"]["value"]
        == "/webhooks/github/argocd"
    )
    assert not any(kind == "Ingress" for kind, _ in resources)


def test_large_crds_use_server_side_apply(resources):
    crds = [
        doc
        for (kind, _), doc in resources.items()
        if kind == "CustomResourceDefinition"
    ]
    assert {doc["metadata"]["name"] for doc in crds} == {
        "applications.argoproj.io",
        "applicationsets.argoproj.io",
        "appprojects.argoproj.io",
    }
    for doc in crds:
        assert (
            "ServerSideApply=true"
            in doc["metadata"]["annotations"]["argocd.argoproj.io/sync-options"]
        )


def test_application_set_lease_permissions_do_not_expand(resources):
    rules = resources["Role", "argocd-applicationset-controller"]["rules"]
    leases = [rule for rule in rules if rule["apiGroups"] == ["coordination.k8s.io"]]
    assert leases == [
        {
            "apiGroups": ["coordination.k8s.io"],
            "resources": ["leases"],
            "verbs": ["create"],
        },
        {
            "apiGroups": ["coordination.k8s.io"],
            "resources": ["leases"],
            "resourceNames": ["58ac56fa.applicationsets.argoproj.io"],
            "verbs": ["get", "update", "create"],
        },
    ]
