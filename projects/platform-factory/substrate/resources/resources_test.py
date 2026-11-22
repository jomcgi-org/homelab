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


def test_microvm_asset_job_is_a_wave_1_sync_hook_with_pinned_images() -> None:
    docs = _docs()
    job = _microvm_job(docs)
    annotations = job["metadata"]["annotations"]
    assert annotations["argocd.argoproj.io/hook"] == "Sync"
    assert annotations["argocd.argoproj.io/hook-delete-policy"] == "BeforeHookCreation"
    # After the bucket Job (wave 0), before the one-off Jobs that use its CLI.
    bucket = _one(docs, "Job", "rustfs-bucket-init")
    assert _wave(bucket) < _wave(job) < _wave(_spike_job(docs))
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
    assert template["resources"]["requests"]["memory"] == "256Mi"
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


_SPIKE = _HERE / "microvm-actor-spike" / "spike.py"
_BOOTSTRAP_OWNERS = (
    "--authz-bootstrap-owners=system:serviceaccount:ate-system:ate-client,"
    "spiffe://cluster.local/ns/kagent/sa/kagent-controller"
)


def _spike_job(docs: list[dict]) -> dict:
    return _one(docs, "Job", "microvm-actor-spike-3")


def test_spike_job_runs_as_ate_client_with_the_api_audience_token() -> None:
    docs = _docs()
    job = _spike_job(docs)
    pod = job["spec"]["template"]["spec"]
    assert job["metadata"]["namespace"] == "ate-system"
    assert pod["serviceAccountName"] == "ate-client"
    # One-off: ArgoCD creates it once. It is not a hook that reruns each sync.
    assert "argocd.argoproj.io/hook" not in job["metadata"]["annotations"]
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] <= 3600
    (token,) = [v for v in pod["volumes"] if v["name"] == "ate-token"]
    (source,) = token["projected"]["sources"]
    audiences = yaml.safe_load(
        _one(docs, "ConfigMap", "ate-api-authentication")["data"]["authentication.yaml"]
    )["jwtProviders"][0]["audiences"]
    assert [source["serviceAccountToken"]["audience"]] == audiences
    # The Kubernetes API token is mounted only for the ClusterTrustBundle list.
    assert pod["automountServiceAccountToken"] is True
    # The container that runs the CLI holds no credential in its env.
    (spike,) = job["spec"]["template"]["spec"]["containers"]
    assert "envFrom" not in spike
    assert all("valueFrom" not in e for e in spike["env"])
    assert not [
        e for e in spike["env"] if "TOKEN" in e["name"] or "SECRET" in e["name"]
    ]
    assert "hostNetwork" not in pod


def test_spike_job_grants_nothing_new() -> None:
    docs = _docs()
    server = _one(docs, "Deployment", "ate-api-server")
    args = server["spec"]["template"]["spec"]["containers"][0]["args"]
    assert _BOOTSTRAP_OWNERS in args
    for doc in docs:
        if doc["kind"] in ("RoleBinding", "ClusterRoleBinding"):
            assert "ate-client" not in json.dumps(doc.get("subjects", [])), doc[
                "metadata"
            ]["name"]


def test_spike_job_is_bounded_pinned_and_locked_down() -> None:
    job = _spike_job(_docs())
    pod = job["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    for container in _containers(job):
        name = container["name"]
        assert "@sha256:" in container["image"], name
        limits = container["resources"]["limits"]
        assert "memory" in limits and "ephemeral-storage" in limits, name
        assert container["resources"]["requests"]["ephemeral-storage"], name
        context = container["securityContext"]
        assert context["allowPrivilegeEscalation"] is False, name
        assert context["capabilities"] == {"drop": ["ALL"]}, name
    for volume in pod["volumes"]:
        if "emptyDir" in volume:
            assert "sizeLimit" in volume["emptyDir"], volume["name"]


def test_spike_job_runs_the_pinned_cli_staged_in_rustfs() -> None:
    docs = _docs()
    job = _spike_job(docs)
    tool = _ASSETS["tools"]["kubectl-ate"]
    assert tool["url"].startswith(
        "https://github.com/kagent-dev/substrate/releases/download/v1.0.1/"
    )
    (fetch,) = job["spec"]["template"]["spec"]["initContainers"]
    env = {e["name"]: e.get("value") for e in fetch["env"]}
    assert env["BUCKET"] == _ASSETS["bucket"]
    assert (
        env["OBJECT_KEY"] == f"{_ASSETS['prefix']}/{_ASSETS['arch']}/{tool['sha256']}"
    )
    # The assets Job stages the CLI beside the sandbox assets.
    config = _one(docs, "ConfigMap", _generated(docs, "microvm-assets"))
    assert json.loads(config["data"]["assets.json"])["tools"] == _ASSETS["tools"]


def _generated(docs: list[dict], prefix: str) -> str:
    (name,) = [
        d["metadata"]["name"]
        for d in docs
        if d["kind"] == "ConfigMap"
        and d["metadata"]["name"].startswith(prefix + "-")
        and not d["metadata"]["name"].startswith(prefix + "-actor")
        and d["metadata"]["name"].count("-") == prefix.count("-") + 1
    ]
    return name


def test_spike_job_mounts_its_script_and_the_template_it_deletes() -> None:
    docs = _docs()
    job = _spike_job(docs)
    pod = job["spec"]["template"]["spec"]
    (config,) = [v for v in pod["volumes"] if v["name"] == "config"]
    data = _one(docs, "ConfigMap", config["configMap"]["name"])["data"]
    assert set(data) == {"spike.py", "template.yaml"}
    template = yaml.safe_load(data["template.yaml"])
    assert template["metadata"] == {"atespace": "microvm-spike", "name": "http"}
    assert template["workerSelector"]["matchLabels"] == {"workload": "microvm-spike"}
    assert template["resources"]["limits"]["memoryBytes"] == 256 * 1024 * 1024
    assert template["snapshotConfig"]["preferredFidelity"] == "SNAPSHOT_FIDELITY_MEMORY"
    (container,) = template["containers"]
    # No new image: the workload is the Job's own python digest.
    (spike,) = job["spec"]["template"]["spec"]["containers"]
    assert container["image"] == spike["image"]
    assert container["image"].endswith(
        "@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f"
    )
    # `command` is the whole argv. Port 8080 needs no CAP_NET_BIND_SERVICE, and
    # the default capability set is kept (no securityContext).
    assert container["command"] == [
        "/usr/local/bin/python3",
        "-m",
        "http.server",
        "8080",
        "--bind",
        "0.0.0.0",
    ]
    assert "securityContext" not in container
    probe = container["wakeupProbe"]
    assert probe["httpGet"] == {"path": "/", "port": 8080}
    assert probe["timeoutSeconds"] == 45
    # The template selects the spike pool.
    pool = _one(docs, "WorkerPool", "microvm-spike")
    assert pool["metadata"]["labels"] == template["workerSelector"]["matchLabels"]


def test_spike_job_reaches_only_the_ate_api_the_kube_api_and_rustfs() -> None:
    docs = _docs()
    policy = _one(docs, "CiliumNetworkPolicy", "microvm-actor-spike")["spec"]
    assert policy["endpointSelector"]["matchLabels"] == {"app": "microvm-actor-spike"}
    assert "ingress" not in policy
    kube, api, rustfs = policy["egress"]
    assert kube["toEntities"] == ["kube-apiserver"]
    assert kube["toPorts"] == [{"ports": [{"port": "6443", "protocol": "TCP"}]}]
    assert api["toEndpoints"] == [{"matchLabels": {"app": "ate-api-server"}}]
    assert api["toPorts"] == [{"ports": [{"port": "443", "protocol": "TCP"}]}]
    assert rustfs["toEndpoints"] == [{"matchLabels": {"app": "rustfs"}}]
    assert rustfs["toPorts"] == [{"ports": [{"port": "9000", "protocol": "TCP"}]}]
    assert not any("toFQDNs" in rule or "toCIDR" in rule for rule in policy["egress"])
    # The API admits this Job's pods, and no other pod in ate-system.
    admit = _one(docs, "CiliumNetworkPolicy", "ate-api-server-microvm-actor-spike")
    (ingress,) = admit["spec"]["ingress"]
    assert ingress["fromEndpoints"] == [{"matchLabels": {"app": "microvm-actor-spike"}}]
    assert ingress["toPorts"] == [{"ports": [{"port": "443", "protocol": "TCP"}]}]
    (rustfs_in,) = _one(docs, "CiliumNetworkPolicy", "rustfs")["spec"]["ingress"]
    (expression,) = rustfs_in["fromEndpoints"][0]["matchExpressions"]
    assert "microvm-actor-spike" in expression["values"]


# A kubectl-ate stand-in: the calls spike.py makes, over a state directory.
_FAKE_CLI = """#!/bin/sh
shift 4
echo "$*" >> "$FAKE_STATE/calls"
state() { cat "$FAKE_STATE/state" 2>/dev/null || echo SUSPENDED; }
case "$*" in
  "get workers --sandbox-class microvm -o json")
    echo '{"workers":[{"metadata":{"name":"w0"},"workerPool":"other","status":{"state":"WORKER_STATE_ACTIVE"}},{"metadata":{"name":"w1"},"workerPool":"microvm-spike","status":{"state":"'"${FAKE_WORKER_STATE:-WORKER_STATE_ACTIVE}"'"}}]}' ;;
  "create atespace microvm-spike"|"create actor-template -f "*) echo created ;;
  "get actor-template http -a microvm-spike -o json")
    n=$(cat "$FAKE_STATE/golden" 2>/dev/null || echo 0); n=$((n+1))
    echo $n > "$FAKE_STATE/golden"
    if [ -n "$FAKE_GOLDEN_ERROR" ]; then
      printf '{"status":{"goldenSnapshotStatus":{"errorMessage":"%s"}}}\n' "$FAKE_GOLDEN_ERROR"
    elif [ "$n" -ge 2 ]; then
      echo '{"status":{"goldenSnapshotStatus":{"goldenTag":{"name":"g1"}}}}'
    else echo '{"status":{}}'; fi ;;
  "create actor spike-1 -a microvm-spike --template http")
    echo SUSPENDED > "$FAKE_STATE/state"; echo created ;;
  "resume actor spike-1 -a microvm-spike") echo RUNNING > "$FAKE_STATE/state"; echo ok ;;
  "suspend actor spike-1 -a microvm-spike --mode sync")
    echo SUSPENDED > "$FAKE_STATE/state"; echo ok ;;
  "get actor spike-1 -a microvm-spike -o json")
    printf '{"status":{"state":"ACTOR_STATE_%s"}}\n' "$(state)" ;;
  delete*) echo deleted ;;
  *) echo "unexpected: $*" >&2; exit 2 ;;
esac
"""


def _run_spike(
    tmp_path: Path, monkeypatch, golden_error: bool = False, env: dict | None = None
):
    import hashlib
    import importlib.util

    cli = tmp_path / "kubectl-ate"
    cli.write_text(_FAKE_CLI)
    cli.chmod(0o755)
    config = tmp_path / "assets.json"
    config.write_text(
        json.dumps(
            {
                "tools": {
                    "kubectl-ate": {
                        "sha256": hashlib.sha256(cli.read_bytes()).hexdigest()
                    }
                }
            }
        )
    )
    state = tmp_path / "state"
    state.mkdir()
    for key, value in {
        "ATE_BIN": cli,
        "ASSETS_CONFIG": config,
        "TEMPLATE_FILE": tmp_path / "template.yaml",
        "FAKE_STATE": state,
        "HOLD_SECONDS": "0",
        "POLL_SECONDS": "0.01",
    }.items():
        monkeypatch.setenv(key, str(value))
    if golden_error:
        monkeypatch.setenv("FAKE_GOLDEN_ERROR", "boom")
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("spike", _SPIKE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, (state / "calls")


def test_spike_script_times_each_phase_and_cleans_up(
    tmp_path, monkeypatch, capsys
) -> None:
    module, calls = _run_spike(tmp_path, monkeypatch)
    assert module.main() == 0
    out = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("SPIKE ")
    ]
    phases = [line.split("phase=")[1].split()[0] for line in out if "phase=" in line]
    assert phases == [
        "worker_wait",
        "golden_build",
        "create_actor",
        "first_start_call",
        "first_start",
        "suspend",
        "restore_call",
        "restore",
    ]
    assert [
        line
        for line in out
        if "event=hold" in line and "name=idle_after_restore" in line
    ]
    summary = out[-1]
    assert "event=summary result=ok" in summary or " result=ok " in summary
    for phase in ("golden_build", "first_start", "suspend", "restore"):
        assert f"{phase}=" in summary
    seen = calls.read_text().splitlines()
    # Cleaned before and after the run, in dependency order.
    deletes = [c for c in seen if c.startswith("delete")]
    assert (
        deletes
        == [
            "delete actor spike-1 -a microvm-spike",
            "delete actor-template http -a microvm-spike",
            "delete atespace microvm-spike",
        ]
        * 2
    )


def test_spike_script_cleans_up_and_fails_when_the_golden_build_fails(
    tmp_path, monkeypatch, capsys
) -> None:
    module, calls = _run_spike(tmp_path, monkeypatch, golden_error=True)
    assert module.main() == 1
    out = capsys.readouterr().out
    assert "result=fail" in out and "golden build failed: boom" in out
    assert "create actor spike-1" not in calls.read_text()
    assert calls.read_text().splitlines()[-1] == "delete atespace microvm-spike"


def test_spike_script_waits_for_its_own_pools_active_worker(
    tmp_path, monkeypatch, capsys
) -> None:
    # Another pool's worker is ACTIVE in the fake; this pool's is not.
    module, calls = _run_spike(
        tmp_path,
        monkeypatch,
        env={
            "FAKE_WORKER_STATE": "WORKER_STATE_UNAVAILABLE",
            "WORKER_WAIT_SECONDS": "0.3",
        },
    )
    assert module.main() == 1
    out = capsys.readouterr().out
    assert "a microvm-spike worker: not ready after" in out
    assert "create atespace" not in calls.read_text()


def test_spike_script_logs_a_long_golden_error_in_full(
    tmp_path, monkeypatch, capsys
) -> None:
    message = "start-" + "x" * 1500 + "-end"
    module, _ = _run_spike(tmp_path, monkeypatch, env={"FAKE_GOLDEN_ERROR": message})
    assert module.main() == 1
    assert message in capsys.readouterr().out
