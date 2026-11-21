"""The atelet's private ghcr.io pull path (HOME-539), as ArgoCD applies it."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent
_IMAGE = (
    "ghcr.io/jomcgi/homelab/projects/platform-factory/substrate/credential-provider"
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


def test_the_receiver_route_keeps_substrate_authorization_and_scoped_mtls() -> None:
    docs = _docs()
    config_name = "atenet-egress-agentgateway-config"
    current = yaml.safe_load(
        _one(docs, "ConfigMap", config_name)["data"]["config.yaml"]
    )
    upstream_path = _HERE / "upstream/substrate/atenet-egress.yaml"
    upstream = [item for item in yaml.safe_load_all(upstream_path.read_text()) if item]
    original = yaml.safe_load(
        _one(upstream, "ConfigMap", config_name)["data"]["config.yaml"]
    )
    routes = current["binds"][0]["listeners"][0]["routes"]
    route = routes.pop(0)
    assert route["name"] == "factory-push-guard"
    host = "factory-push-guard.factory-r-homelab.svc.cluster.local"
    assert route["hostnames"] == [host]
    assert route["backends"] == [
        {
            "host": f"{host}:443",
            "policies": {
                "backendTLS": {
                    "hostname": host,
                    "cert": "/run/podidentity.podcert.ate.dev/credential-bundle.pem",
                    "key": "/run/podidentity.podcert.ate.dev/credential-bundle.pem",
                    "root": "/run/servicedns.podcert.ate.dev/trust-bundle.pem",
                }
            },
        }
    ]
    assert route["policies"] == routes[0]["policies"]
    assert "substrateEgress" in route["policies"]
    assert current == original
