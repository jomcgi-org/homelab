"""Validate the Substrate resources rendered for ArgoCD: the atelet's ghcr.io
pull path, the egress scanner pilot and the microVM sandbox class."""

from __future__ import annotations

import json
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


_ASSETS = json.loads((_HERE / "microvm-assets" / "assets.json").read_text())
# The amd64 values in manifests/ate-install/sandboxconfig-microvm.yaml.tmpl of
# kagent-dev/substrate v1.0.1 (and agent-substrate/substrate main).
_UPSTREAM_AMD64 = {
    "cloud-hypervisor": "448af3d4e59b22c2987f7df94c213ad40fb53a10d437e42b5ee6c4fce7c29ecc",
    "virtiofsd": "15b2e72a78cc08a9bd8a6943e89fb69c88cb3cbeb63069efceade835342ac7d4",
    "kata-kernel": "f87d8359035cb7557b16a319e6431ea09b183e62268aef436bf8dc6530d786b6",
    "kata-image": "6770db2cca5710593f27c9ac5e1d9d7aab836422695a5e5c4807496a92a0f24d",
}
_SPIKE_NS = "substrate-microvm-spike"


def _docs() -> list[dict]:
    out = subprocess.run(
        [os.environ["KUSTOMIZE_BIN"], "build", str(_HERE)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _scanner() -> dict:
    manifest = Path(os.environ["SCANNER_MANIFEST"]).read_text()
    return _one(
        [doc for doc in yaml.safe_load_all(manifest) if doc],
        "Deployment",
        "egress-scan",
    )


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


def _mounted_gateway_config(docs: list[dict]) -> dict:
    gateway = _one(docs, "Deployment", "atenet-egress")
    volumes = gateway["spec"]["template"]["spec"]["volumes"]
    mounted = next(v for v in volumes if v["name"] == "config")["configMap"]["name"]
    assert mounted.startswith("atenet-egress-guard-config-")
    return _one(docs, "ConfigMap", mounted)


def test_egress_scan_pilot_changes_only_hindsight_route() -> None:
    applied = _mounted_gateway_config(_docs())
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
    assert config == yaml.safe_load((_HERE / "atenet-egress-config.yaml").read_text())


def test_egress_gateway_and_its_config_wait_for_the_scanner() -> None:
    docs = _docs()
    scanner = _scanner()
    gateway = _one(docs, "Deployment", "atenet-egress")
    config = _mounted_gateway_config(docs)
    assert _wave(scanner) < _wave(config) == _wave(gateway)


def test_egress_scanner_has_warm_replicas_and_a_gateway_only_listener() -> None:
    docs = _docs()
    spec = _scanner()["spec"]
    assert spec["replicas"] == 2
    pod = spec["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["runtimeClassName"] == "gvisor"
    (container,) = pod["containers"]
    image, _, digest = container["image"].partition("@")
    assert image == _SCANNER_IMAGE
    assert re.fullmatch(r"sha256:[a-f0-9]{64}", digest)
    image_values = yaml.safe_load(Path(os.environ["SCANNER_VALUES"]).read_text())
    assert digest == image_values["image"]["digest"]
    assert container["ports"] == [
        {"name": "grpc", "containerPort": 8080},
        {"name": "metrics", "containerPort": 9090},
    ]
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


def test_substrate_sync_uses_the_chart_as_the_only_scanner_deployment() -> None:
    app = yaml.safe_load(Path(os.environ["SCANNER_APPLICATION"]).read_text())
    assert app["metadata"]["name"] == "substrate"
    chart, resources = app["spec"]["sources"]
    assert chart["repoURL"] == "ghcr.io/jomcgi/homelab/charts"
    assert chart["chart"] == chart["helm"]["releaseName"] == "egress-scan"
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", chart["targetRevision"])
    assert resources["path"] == "projects/platform-factory/substrate/resources"
    assert resources["targetRevision"] == "HEAD"
    assert (
        _scanner()["metadata"]["namespace"] == app["spec"]["destination"]["namespace"]
    )
    assert not any(
        d["kind"] == "Deployment" and d["metadata"]["name"] == "egress-scan"
        for d in _docs()
    )


def test_the_receiver_route_keeps_substrate_authorization_and_scoped_mtls() -> None:
    docs = _docs()
    config_name = "atenet-egress-agentgateway-config"
    current = yaml.safe_load((_HERE / "atenet-egress-config.yaml").read_text())
    mounted = yaml.safe_load(_mounted_gateway_config(docs)["data"]["config.yaml"])
    assert mounted["binds"][0]["listeners"][0] == current["binds"][0]["listeners"][0]
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


def _microvm_job(docs: list[dict]) -> dict:
    return _one(docs, "Job", "microvm-assets-init")


def _containers(job: dict) -> list[dict]:
    pod = job["spec"]["template"]["spec"]
    return pod["initContainers"] + pod["containers"]


def test_microvm_sandboxconfig_pins_the_upstream_amd64_assets() -> None:
    docs = _docs()
    config = _one(docs, "SandboxConfig", "microvm")
    assert config["spec"]["sandboxClass"] == "microvm"
    assert config["spec"]["defaultVersion"] == "v2"
    (version,) = config["spec"]["versions"]
    assert version["name"] == "v2"
    assert version["state"] == "Enabled"
    assert list(version["assets"]) == ["amd64"]
    assets = version["assets"]["amd64"]
    assert {n: a["sha256"] for n, a in assets.items()} == _UPSTREAM_AMD64
    bucket, prefix = _ASSETS["bucket"], _ASSETS["prefix"]
    for name, asset in assets.items():
        # Digest-keyed, and outside the /atespaces/ snapshot prefix the
        # objectstore plugin refuses to serve as assets.
        assert asset["url"] == f"s3://{bucket}/{prefix}/amd64/{asset['sha256']}", name
        assert "/atespaces/" not in asset["url"]
    # The staging Job's pins are the SandboxConfig's pins.
    assert {n: a["sha256"] for n, a in _ASSETS["assets"].items()} == _UPSTREAM_AMD64
    # gVisor is untouched: its class default stays the only gvisor config.
    names = {d["metadata"]["name"] for d in docs if d["kind"] == "SandboxConfig"}
    assert names == {"gvisor-default", "microvm"}


def test_microvm_assets_come_from_pinned_official_releases() -> None:
    assets = _ASSETS["assets"]
    assert _ASSETS["kata"]["url"].startswith(
        "https://github.com/kata-containers/kata-containers/releases/download/4.3.1/"
    )
    assert assets["cloud-hypervisor"]["url"] == (
        "https://github.com/cloud-hypervisor/cloud-hypervisor/releases/download/"
        "v53.0/cloud-hypervisor-static"
    )
    kata = {n: a["kata"] for n, a in assets.items() if "kata" in a}
    assert set(kata) == {"virtiofsd", "kata-kernel", "kata-image"}
    assert all("sha256" in a for a in assets.values())


def test_microvm_asset_job_is_a_postsync_hook_with_pinned_images() -> None:
    job = _microvm_job(_docs())
    annotations = job["metadata"]["annotations"]
    assert annotations["argocd.argoproj.io/hook"] == "PostSync"
    assert annotations["argocd.argoproj.io/hook-delete-policy"] == "BeforeHookCreation"
    for container in _containers(job):
        assert "@sha256:" in container["image"], container["name"]
        assert not container["image"].endswith(":latest")


def test_microvm_asset_job_is_bounded_and_locked_down() -> None:
    pod = _microvm_job(_docs())["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    for container in pod["initContainers"] + pod["containers"]:
        name = container["name"]
        limits = container["resources"]["limits"]
        assert "memory" in limits and "ephemeral-storage" in limits, name
        assert container["resources"]["requests"]["ephemeral-storage"], name
        context = container["securityContext"]
        assert context["allowPrivilegeEscalation"] is False, name
        assert context["capabilities"] == {"drop": ["ALL"]}, name
    (work,) = [v for v in pod["volumes"] if v["name"] == "work"]
    assert "sizeLimit" in work["emptyDir"]


def test_microvm_asset_job_mounts_its_generated_config_and_matches_the_pins() -> None:
    docs = _docs()
    job = _microvm_job(docs)
    (volume,) = [
        v for v in job["spec"]["template"]["spec"]["volumes"] if v["name"] == "config"
    ]
    config = _one(docs, "ConfigMap", volume["configMap"]["name"])
    assert set(config["data"]) == {"assets.json", "stage.py"}
    assert json.loads(config["data"]["assets.json"]) == _ASSETS
    # The shell steps take the bucket and key prefix from env, not assets.json.
    for container in _containers(job):
        env = {e["name"]: e["value"] for e in container.get("env", []) if "value" in e}
        if "BUCKET" in env:
            assert env["BUCKET"] == _ASSETS["bucket"]
            assert env["PREFIX"] == _ASSETS["prefix"]
            assert env["ARCH"] == _ASSETS["arch"]
    # The bucket is the snapshot bucket rustfs-bucket-init creates.
    init = _one(docs, "Job", "rustfs-bucket-init")
    assert f"--bucket {_ASSETS['bucket']} " in json.dumps(init)


def test_microvm_asset_job_reaches_only_rustfs_and_github_releases() -> None:
    docs = _docs()
    policy = _one(docs, "CiliumNetworkPolicy", "microvm-assets-init")["spec"]
    assert policy["endpointSelector"]["matchLabels"] == {"app": "microvm-assets-init"}
    assert "ingress" not in policy
    rustfs, github = policy["egress"]
    assert rustfs["toEndpoints"] == [{"matchLabels": {"app": "rustfs"}}]
    assert rustfs["toPorts"] == [{"ports": [{"port": "9000", "protocol": "TCP"}]}]
    assert {f["matchName"] for f in github["toFQDNs"]} == {
        "github.com",
        "release-assets.githubusercontent.com",
        "objects.githubusercontent.com",
    }
    assert github["toPorts"] == [{"ports": [{"port": "443", "protocol": "TCP"}]}]
    pod_labels = _microvm_job(docs)["spec"]["template"]["metadata"]["labels"]
    assert pod_labels == {"app": "microvm-assets-init"}
    (admit,) = _one(docs, "CiliumNetworkPolicy", "rustfs")["spec"]["ingress"]
    (expression,) = admit["fromEndpoints"][0]["matchExpressions"]
    assert "microvm-assets-init" in expression["values"]


def test_microvm_spike_pool_runs_one_pinned_microvm_worker() -> None:
    docs = _docs()
    namespace = _one(docs, "Namespace", _SPIKE_NS)
    assert (
        namespace["metadata"]["labels"]["factory.jomcgi.dev/substrate-workers"]
        == "true"
    )
    pool = _one(docs, "WorkerPool", "microvm-spike")
    assert pool["metadata"]["namespace"] == _SPIKE_NS
    assert pool["metadata"]["labels"] == {"workload": "microvm-spike"}
    assert pool["spec"]["replicas"] == 1
    assert pool["spec"]["sandboxClasses"] == [
        {"name": "microvm", "configRef": {"name": "microvm"}}
    ]
    assert pool["spec"]["workerImage"] == (
        "ghcr.io/kagent-dev/substrate/ateworker-microvm:v1.0.1"
        "@sha256:0090d0420d34537e885e8453cf3dccf57e6e479a77fc736d29c417ef05e486c3"
    )
    template = pool["spec"]["template"]
    account = _one(docs, "ServiceAccount", template["serviceAccountName"])
    assert account["metadata"]["namespace"] == _SPIKE_NS
    assert account["automountServiceAccountToken"] is False
    assert template["resources"]["limits"]["memory"] == "1Gi"
    # No gVisor pool shares the spike namespace.
    pools = [d for d in docs if d["kind"] == "WorkerPool"]
    assert [p["metadata"]["namespace"] for p in pools] == [_SPIKE_NS]


def test_microvm_spike_namespace_allows_only_the_router_and_the_gateway() -> None:
    policy = _one(_docs(), "CiliumNetworkPolicy", "substrate-workers")["spec"]
    (ingress,) = policy["ingress"]
    (egress,) = policy["egress"]
    assert ingress["fromEndpoints"] == [
        {
            "matchLabels": {
                "k8s:io.kubernetes.pod.namespace": "ate-system",
                "app": "atenet-router",
            }
        }
    ]
    assert egress["toEndpoints"] == [
        {
            "matchLabels": {
                "k8s:io.kubernetes.pod.namespace": "ate-system",
                "app": "atenet-egress",
            }
        }
    ]
    for rule in (ingress, egress):
        assert rule["toPorts"] == [{"ports": [{"port": "8443", "protocol": "TCP"}]}]
