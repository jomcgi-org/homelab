"""Validate the Substrate resources rendered for ArgoCD."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent
_IMAGE = (
    "ghcr.io/jomcgi/homelab/projects/platform-factory/substrate/credential-provider"
)
_SCANNER_IMAGE = (
    "ghcr.io/jomcgi/homelab/projects/platform-factory/substrate/egress-scan"
)


def _docs() -> list[dict]:
    out = subprocess.run(
        [os.environ["KUSTOMIZE_BIN"], "build", str(_HERE)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _one(docs: list[dict], kind: str, name: str) -> dict:
    (found,) = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    return found


def _wave(doc: dict) -> int:
    annotations = doc["metadata"].get("annotations", {})
    return int(annotations.get("argocd.argoproj.io/sync-wave", "0"))


def _atelet(docs: list[dict]) -> dict:
    return _one(docs, "DaemonSet", "atelet")["spec"]["template"]["spec"]


def test_plugin_directory_is_an_emptydir_filled_from_the_pinned_image() -> None:
    pod = _atelet(_docs())
    (bin_volume,) = [
        v for v in pod["volumes"] if v["name"] == "image-credential-provider-bin"
    ]
    assert bin_volume == {"name": "image-credential-provider-bin", "emptyDir": {}}
    (install,) = [
        c for c in pod["initContainers"] if c["name"] == "install-credential-provider"
    ]
    digest = Path(os.environ["PROVIDER_DIGEST"]).read_text().strip()
    assert install["image"] == f"{_IMAGE}@{digest}"
    assert install["args"] == ["install", "/plugins/ghcr-credential-provider"]
    assert install["volumeMounts"] == [
        {"name": "image-credential-provider-bin", "mountPath": "/plugins"}
    ]
    assert {"name": "ghcr-imagepull-secret"} in pod["imagePullSecrets"]


def test_only_the_atelet_container_mounts_the_login() -> None:
    pod = _atelet(_docs())
    (secret,) = [v for v in pod["volumes"] if v.get("secret")]
    assert secret["name"] == "ghcr-pull"
    assert secret["secret"]["secretName"] == "ghcr-imagepull-secret"
    holders = {
        c["name"]: m
        for c in pod["containers"] + pod["initContainers"]
        for m in c.get("volumeMounts", [])
        if m["name"] == "ghcr-pull"
    }
    assert holders == {
        "atelet": {"name": "ghcr-pull", "mountPath": "/run/ghcr-pull", "readOnly": True}
    }


def test_plugin_config_names_the_installed_binary_and_the_login() -> None:
    docs = _docs()
    config = yaml.safe_load(
        _one(docs, "ConfigMap", "atelet-image-credential-provider")["data"][
            "config.yaml"
        ]
    )
    (provider,) = config["providers"]
    assert provider["name"] == "ghcr-credential-provider"
    assert provider["matchImages"] == ["ghcr.io"]
    assert provider["env"] == [
        {"name": "DOCKER_CONFIG_FILE", "value": "/run/ghcr-pull/.dockerconfigjson"}
    ]
    item = _one(docs, "OnePasswordItem", "ghcr-imagepull-secret")
    assert item["type"] == "kubernetes.io/dockerconfigjson"
    assert item["metadata"]["namespace"] == "ate-system"
    assert item["spec"] == {
        "itemPath": "vaults/k8s-homelab/items/ghcr-read-permissions"
    }
    # Not before the Namespace's wave, or a fresh install cannot create it.
    namespace = _one(docs, "Namespace", "ate-system")
    assert _wave(item) >= _wave(namespace)


def test_egress_scan_pilot_changes_only_hindsight_route() -> None:
    applied = _one(_docs(), "ConfigMap", "atenet-egress-agentgateway-config")
    raw = _one(
        [
            doc
            for doc in yaml.safe_load_all(
                (_HERE / "upstream" / "substrate" / "atenet-egress.yaml").read_text()
            )
            if doc
        ],
        "ConfigMap",
        "atenet-egress-agentgateway-config",
    )
    config = yaml.safe_load(applied["data"]["config.yaml"])
    (route,) = next(
        listener
        for bind in config["binds"]
        if bind.get("mode") == "internal"
        for listener in bind["listeners"]
        if listener["protocol"] == "HTTP"
    )["routes"]
    (check,) = route["policies"].pop("extAuthz")["conditional"]
    assert check["condition"] == (
        'destination.hostname == "hindsight-api.hindsight.svc.cluster.local"'
        " && destination.port == 8888"
    )
    assert check["host"] == "egress-scan.ate-system.svc:8080"
    assert check["failureMode"] == "deny"
    assert check["protocol"] == {"grpc": {}}
    assert check["includeRequestBody"] == {
        "maxRequestBytes": 524288,
        "allowPartialMessage": False,
        "packAsBytes": True,
    }
    assert check["policies"] == {"http": {"requestTimeout": "80ms"}}
    assert config == yaml.safe_load(raw["data"]["config.yaml"])
    assert _wave(applied) > _wave(_one(_docs(), "Deployment", "egress-scan"))


def test_egress_scanner_has_warm_replicas_and_a_gateway_only_listener() -> None:
    docs = _docs()
    spec = _one(docs, "Deployment", "egress-scan")["spec"]
    assert spec["replicas"] == 2
    pod = spec["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["runtimeClassName"] == "gvisor"
    (container,) = pod["containers"]
    image, _, digest = container["image"].partition("@")
    assert image == _SCANNER_IMAGE
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
    assert container["readinessProbe"]["grpc"] == {"port": 8080}
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert pod["imagePullSecrets"] == [{"name": "ghcr-imagepull-secret"}]
    (ingress,) = _one(docs, "CiliumNetworkPolicy", "egress-scan")["spec"]["ingress"]
    (peer,) = ingress["fromEndpoints"]
    assert peer["matchLabels"] == {"app": "atenet-egress"}
    assert ingress["toPorts"] == [{"ports": [{"port": "8080", "protocol": "TCP"}]}]
    egress_policy = _one(docs, "CiliumNetworkPolicy", "atenet-egress-to-scanner")[
        "spec"
    ]
    assert egress_policy["endpointSelector"]["matchLabels"] == {"app": "atenet-egress"}
    assert egress_policy["egress"] == [
        {
            "toEndpoints": [{"matchLabels": {"app": "egress-scan"}}],
            "toPorts": ingress["toPorts"],
        }
    ]
