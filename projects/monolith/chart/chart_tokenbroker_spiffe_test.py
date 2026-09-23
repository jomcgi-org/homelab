"""Render checks for the monolith token broker SPIFFE client."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml


def _chart_dir() -> Path:
    chart = Path(__file__).resolve().parent
    if not (chart / "Chart.yaml").exists():
        raise RuntimeError("Could not find chart Chart.yaml")
    return chart


def _render(release: str, settings: list[str] | None = None) -> list[dict]:
    helm_bin = os.environ.get("HELM_BIN", "helm")
    argv = [
        helm_bin,
        "template",
        release,
        str(_chart_dir()),
        "--namespace",
        release,
    ]
    for setting in settings or []:
        argv += ["--set", setting]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _object(objects: list[dict], kind: str, name: str) -> dict:
    matches = [
        obj
        for obj in objects
        if obj.get("kind") == kind and obj.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1, (kind, name, len(matches))
    return matches[0]


def _container(deployment: dict, name: str) -> dict:
    containers = deployment["spec"]["template"]["spec"]["containers"]
    return next(container for container in containers if container["name"] == name)


def _env(container: dict) -> dict[str, str]:
    return {
        item["name"]: str(item["value"])
        for item in container.get("env", [])
        if "value" in item
    }


def test_default_render_preserves_plaintext_and_omits_spiffe_runtime() -> None:
    objects = _render("monolith")
    deployment = _object(objects, "Deployment", "monolith")
    backend = _container(deployment, "backend")

    assert _env(backend)["EMBER_TOKENBROKER_URL"].startswith("http://")
    assert _env(backend)["EMBER_TOKENBROKER_SPIFFE_ENABLED"] == "false"
    assert not any(
        container["name"] == "tokenbroker-spiffe-helper"
        for container in deployment["spec"]["template"]["spec"]["containers"]
    )
    assert not any(
        obj.get("metadata", {}).get("name") == "monolith-tokenbroker-spiffe-helper"
        for obj in objects
    )


def test_enabled_render_packages_helper_svid_and_exact_server_identity() -> None:
    objects = _render(
        "monolith",
        [
            "tokenBroker.spiffe.enabled=true",
            "ciliumPolicy.egress.enabled=true",
        ],
    )
    deployment = _object(objects, "Deployment", "monolith")
    pod = deployment["spec"]["template"]["spec"]
    backend = _container(deployment, "backend")
    helper = _container(deployment, "tokenbroker-spiffe-helper")
    env = _env(backend)

    assert env["EMBER_TOKENBROKER_URL"] == (
        "https://embervm-embervm-tokenbroker.embervm.svc:8443"
    )
    assert env["EMBER_TOKENBROKER_SPIFFE_ENABLED"] == "true"
    assert env["EMBER_TOKENBROKER_SPIFFE_ID"] == (
        "spiffe://embervm.jomcgi.dev/ns/embervm/sa/embervm-embervm-tokenbroker"
    )
    assert helper["image"] == "ghcr.io/spiffe/spiffe-helper:0.11.0"
    assert helper["args"] == ["-config", "/etc/spiffe-helper/helper.conf"]
    assert helper["securityContext"]["runAsUser"] == 65532
    assert helper["securityContext"]["runAsNonRoot"] is True
    assert helper["securityContext"]["readOnlyRootFilesystem"] is True
    assert helper["readinessProbe"]["httpGet"] == {
        "path": "/ready",
        "port": "spiffe-health",
    }
    assert pod["securityContext"]["fsGroup"] == 65532

    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    assert volumes["spiffe-workload-api"]["csi"] == {
        "driver": "csi.spiffe.io",
        "readOnly": True,
    }
    assert volumes["tokenbroker-svid"]["emptyDir"] == {
        "medium": "Memory",
        "sizeLimit": "1Mi",
    }
    backend_mount = next(
        mount
        for mount in backend["volumeMounts"]
        if mount["name"] == "tokenbroker-svid"
    )
    assert backend_mount["readOnly"] is True

    config = _object(objects, "ConfigMap", "monolith-tokenbroker-spiffe-helper")[
        "data"
    ]["helper.conf"]
    assert 'hint = "monolith-platform"' in config
    assert "cert_file_mode = 0444" in config
    assert "key_file_mode = 0440" in config
    assert 'agent_address = "/spiffe-workload-api/spire-agent.sock"' in config

    policy = _object(objects, "CiliumNetworkPolicy", "monolith-app-egress")
    broker_rules = [
        rule
        for rule in policy["spec"]["egress"]
        if any(
            endpoint.get("matchLabels", {}).get("app.kubernetes.io/component")
            == "tokenbroker"
            for endpoint in rule.get("toEndpoints", [])
        )
    ]
    assert len(broker_rules) == 1
    assert broker_rules[0]["toPorts"][0]["ports"] == [
        {"port": "8443", "protocol": "TCP"}
    ]


def test_enabled_render_rejects_non_tls_url_and_missing_server_identity() -> None:
    for settings, message in [
        (
            [
                "tokenBroker.spiffe.enabled=true",
                "tokenBroker.spiffe.url=http://broker:8080",
            ],
            "must use https",
        ),
        (
            [
                "tokenBroker.spiffe.enabled=true",
                "tokenBroker.spiffe.serverId=",
            ],
            "serverId is required",
        ),
    ]:
        try:
            _render("invalid", settings)
        except RuntimeError as error:
            assert message in str(error)
        else:
            raise AssertionError(f"accepted invalid SPIFFE settings: {settings}")


def test_all_checked_in_environment_overlays_keep_spiffe_default_off() -> None:
    projects = _chart_dir().parents[1]
    paths = [
        _chart_dir() / "values.yaml",
        projects / "monolith/deploy/values.yaml",
        projects / "monolith/deploy/values-gke.yaml",
        projects / "monolith/dev/deploy/values.yaml",
        projects / "monolith/dev/deploy/values-recovery-gke.yaml",
    ]
    for path in paths:
        values = yaml.safe_load(path.read_text())
        assert values["tokenBroker"]["spiffe"]["enabled"] is False, path
