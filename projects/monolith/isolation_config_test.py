"""Rendered configuration guards for ADR security/004 isolation."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml


def _chart_dir() -> Path:
    return Path(__file__).resolve().parent / "chart"


def _path(name: str, fallback: Path) -> Path:
    return Path(os.environ.get(name) or fallback)


def _deploy_values() -> Path:
    return _path("DEPLOY_VALUES", _chart_dir().parent / "deploy" / "values.yaml")


def _gke_values() -> Path:
    return _path("GKE_VALUES", _chart_dir().parent / "deploy" / "values-gke.yaml")


def _public_values() -> Path:
    return _path(
        "PUBLIC_CHART_VALUES",
        _chart_dir().parents[1] / "monolith-public" / "chart" / "values.yaml",
    )


def _public_deploy_values() -> Path:
    return _path(
        "PUBLIC_DEPLOY_VALUES",
        _chart_dir().parents[1] / "monolith-public" / "deploy" / "values.yaml",
    )


def _public_gke_values() -> Path:
    return _path(
        "PUBLIC_GKE_VALUES",
        _chart_dir().parents[1] / "monolith-public" / "deploy" / "values-gke.yaml",
    )


def _render(*sets: str, gke: bool = False) -> str:
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "monolith",
        str(_chart_dir()),
        "--namespace",
        "monolith",
        "--values",
        str(_chart_dir() / "values.yaml"),
        "--values",
        str(_deploy_values()),
    ]
    if gke:
        argv += ["--values", str(_gke_values())]
    for value in sets:
        argv += ["--set", value]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return result.stdout


def _render_public(*, gke: bool = False) -> str:
    chart = _public_values().parent
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "monolith-public",
        str(chart),
        "--namespace",
        "monolith-public",
        "--values",
        str(_public_values()),
        "--values",
        str(_public_deploy_values()),
    ]
    if gke:
        argv += ["--values", str(_public_gke_values())]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"public helm template failed: {result.stderr}")
    return result.stdout


def _docs(rendered: str):
    for chunk in re.split(r"^---\s*$", rendered, flags=re.MULTILINE):
        doc = yaml.safe_load(chunk)
        if isinstance(doc, dict):
            yield doc


def _named_doc(rendered: str, kind: str, name: str) -> dict:
    matches = [
        doc
        for doc in _docs(rendered)
        if doc.get("kind") == kind and doc.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1, f"expected one {kind} {name}, found {len(matches)}"
    return matches[0]


def test_public_reads_are_pinned_to_standby_service_and_role():
    private_deploy = yaml.safe_load(_deploy_values().read_text())
    public_chart = yaml.safe_load(_public_values().read_text())
    public_deploy = yaml.safe_load(_public_deploy_values().read_text())

    assert private_deploy["postgres"]["instances"] == 2
    assert public_deploy["onepassword"]["itemPath"].endswith("/public-reader-db")
    assert public_deploy["publicReaderDatabase"] == {
        "serviceName": "monolith-pg-ro",
        "username": "public_reader",
    }
    assert public_chart["publicReaderDatabase"] == public_deploy["publicReaderDatabase"]

    web_env = {entry["name"]: entry for entry in public_chart["web"]["env"]}
    assert web_env["DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": public_chart["databaseSecretName"],
        "key": "uri",
    }
    assert web_env["PUBLIC_READER_DATABASE_SERVICE"]["value"] == "monolith-pg-ro"
    assert web_env["PUBLIC_READER_DATABASE_USER"]["value"] == "public_reader"
    assert (
        web_env["PUBLIC_WRITER_DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"]
        != public_chart["databaseSecretName"]
    )

    for rendered in (_render_public(), _render_public(gke=True)):
        deployments = [
            doc for doc in _docs(rendered) if doc.get("kind") == "Deployment"
        ]
        public_web = [
            doc
            for doc in deployments
            if doc["spec"]["template"]["metadata"]["labels"].get(
                "app.kubernetes.io/component"
            )
            == "web"
        ]
        assert len(public_web) == 1
        rendered_env = {
            entry["name"]: entry
            for entry in public_web[0]["spec"]["template"]["spec"]["containers"][0][
                "env"
            ]
        }
        assert (
            rendered_env["PUBLIC_READER_DATABASE_SERVICE"]["value"] == "monolith-pg-ro"
        )
        assert rendered_env["PUBLIC_READER_DATABASE_USER"]["value"] == "public_reader"


def test_private_app_containers_have_read_only_roots_and_private_tmp_mounts():
    deployment = _named_doc(_render(), "Deployment", "monolith")
    pod_spec = deployment["spec"]["template"]["spec"]
    containers = {container["name"]: container for container in pod_spec["containers"]}

    expected_mounts = {
        "backend": "backend-tmp",
        "progress-ingest": "progress-tmp",
        "frontend": "frontend-tmp",
    }
    assert set(containers) == set(expected_mounts)
    for name, volume_name in expected_mounts.items():
        assert containers[name]["securityContext"]["readOnlyRootFilesystem"] is True
        assert containers[name]["volumeMounts"] == [
            {"name": volume_name, "mountPath": "/tmp"}
        ]

    volumes = {volume["name"]: volume for volume in pod_spec["volumes"]}
    assert set(volumes) == set(expected_mounts.values())
    assert all("sizeLimit" in volume["emptyDir"] for volume in volumes.values())
    assert pod_spec["securityContext"]["fsGroup"] == 65532


def test_optional_whatsapp_container_keeps_the_same_filesystem_boundary():
    deployment = _named_doc(
        _render("whatsapp.enabled=true"), "Deployment", "monolith-whatsapp"
    )
    pod_spec = deployment["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["volumeMounts"] == [{"name": "whatsapp-tmp", "mountPath": "/tmp"}]
    assert pod_spec["volumes"] == [
        {"name": "whatsapp-tmp", "emptyDir": {"sizeLimit": "32Mi"}}
    ]


def test_egress_policy_is_off_by_default_and_on_gke():
    assert "monolith-app-egress" not in _render()
    assert "monolith-app-egress" not in _render(gke=True)
    gke = yaml.safe_load(_gke_values().read_text())
    assert gke["ciliumPolicy"]["egress"] == {
        "enabled": False,
        "enforce": False,
    }


def test_egress_audit_and_enforce_shapes_keep_legitimate_flows_scoped():
    audit = _named_doc(
        _render("ciliumPolicy.egress.enabled=true"),
        "CiliumNetworkPolicy",
        "monolith-app-egress",
    )
    assert audit["spec"]["enableDefaultDeny"]["egress"] is False

    enforced = _named_doc(
        _render(
            "ciliumPolicy.egress.enabled=true",
            "ciliumPolicy.egress.enforce=true",
        ),
        "CiliumNetworkPolicy",
        "monolith-app-egress",
    )
    spec = enforced["spec"]
    assert spec["enableDefaultDeny"]["egress"] is True
    assert spec["endpointSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "monolith",
        "app.kubernetes.io/instance": "monolith",
        "app.kubernetes.io/component": "app",
    }

    rules = spec["egress"]
    assert not any("toCIDR" in key or "toCIDRSet" in key for r in rules for key in r)
    assert not any("world" in r.get("toEntities", []) for r in rules)

    endpoint_ports = set()
    for rule in rules:
        ports = {
            port["port"]
            for group in rule.get("toPorts", [])
            for port in group.get("ports", [])
        }
        for selector in rule.get("toEndpoints", []):
            labels = selector.get("matchLabels", {})
            endpoint_ports.add((frozenset(labels.items()), frozenset(ports)))

    required = [
        ({"cnpg.io/cluster": "monolith-pg"}, "5432"),
        ({"app.kubernetes.io/name": "authentik"}, "80"),
        ({"app.kubernetes.io/name": "otel-collector"}, "4318"),
        ({"app.kubernetes.io/component": "inference"}, "8080"),
        ({"app.kubernetes.io/component": "embeddings"}, "8080"),
        ({"app.kubernetes.io/component": "searxng"}, "8080"),
        ({"app.kubernetes.io/name": "embervm"}, "8080"),
        ({"app.kubernetes.io/component": "tokenbroker"}, "8080"),
        ({"app.kubernetes.io/name": "embervm-serving-envoy"}, "5401"),
        ({"k8s:app": "nvidia-dcgm-exporter"}, "9400"),
        ({"tailscale.com/parent-resource": "inference-bridge-freetoken"}, "8090"),
    ]
    for labels, port in required:
        assert any(
            labels.items() <= dict(selector).items() and port in ports
            for selector, ports in endpoint_ports
        ), f"missing scoped egress for {labels} on {port}"

    api_rules = [r for r in rules if "kube-apiserver" in r.get("toEntities", [])]
    assert len(api_rules) == 1
    assert {
        port["port"] for group in api_rules[0]["toPorts"] for port in group["ports"]
    } == {"443", "6443"}

    fqdn_rules = [rule for rule in rules if "toFQDNs" in rule]
    assert len(fqdn_rules) == 1
    names = {entry["matchName"] for entry in fqdn_rules[0]["toFQDNs"]}
    assert {
        "api.github.com",
        "openrouter.ai",
        "api.deepseek.com",
        "integrate.api.nvidia.com",
        "api.meta.ai",
        "cdn.discordapp.com",
        "media.discordapp.net",
        "stream.aisstream.io",
        "7c56b458cd657d96b095c63d181c051f.r2.cloudflarestorage.com",
    } <= names
    assert all("*" not in name for name in names)


def test_enforcement_cannot_be_enabled_without_rendering_the_policy():
    with pytest.raises(RuntimeError, match="enforce requires"):
        _render("ciliumPolicy.egress.enforce=true")
