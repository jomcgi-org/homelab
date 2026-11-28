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


def _internal_listener(config: dict, protocol: str) -> dict:
    return next(
        listener
        for bind in config["binds"]
        if bind.get("mode") == "internal"
        for listener in bind["listeners"]
        if listener["protocol"] == protocol
    )


def test_egress_scan_changes_only_the_hindsight_and_abliteration_routes() -> None:
    applied = _mounted_gateway_config(_docs())
    config = yaml.safe_load(applied["data"]["config.yaml"])
    # HOME-938: the terminated HTTPS catch-all route scans abliteration.ai.
    (tls_route,) = [
        r for r in _internal_listener(config, "HTTPS")["routes"] if "name" not in r
    ]
    (tls_check,) = tls_route["policies"].pop("extAuthz")["conditional"]
    assert tls_check["condition"] == (
        'destination.hostname == "api.abliteration.ai" && destination.port == 443'
    )
    assert tls_check["failureMode"] == "deny"
    assert tls_check["protocol"]["grpc"]["context"] == {"route": "abliteration"}
    (route,) = _internal_listener(config, "HTTP")["routes"]
    (check,) = route["policies"].pop("extAuthz")["conditional"]
    for key in ("host", "includeRequestBody", "policies"):
        assert tls_check[key] == check[key], key
    assert check["condition"] == (
        'destination.hostname == "hindsight-api.hindsight.svc.cluster.local"'
        " && destination.port == 8888"
    )
    assert check["host"] == "egress-scan.ate-system.svc:8080"
    assert check["failureMode"] == "deny"
    assert check["protocol"] == {
        "grpc": {
            "context": {"route": "hindsight"},
            "metadata": {"actor": '{"spiffe": source.spiffeId}'},
        }
    }
    assert check["includeRequestBody"] == {
        "maxRequestBytes": 524288,
        "allowPartialMessage": False,
        "packAsBytes": True,
    }
    assert check["policies"] == {"http": {"requestTimeout": "80ms"}}
    assert config == yaml.safe_load((_HERE / "atenet-egress-config.yaml").read_text())


def test_the_metric_route_allowlist_names_the_scanned_hosts() -> None:
    # The route label of the scanner metrics is bounded by an allowlist, and a
    # scanned host outside it would count as "other" for ever.
    scanned = set()
    for name in ("gateway-policy.yaml", "gateway-policy-tls.yaml"):
        policy = yaml.safe_load((_HERE / "egress-scan" / name).read_text())
        for check in policy["conditional"]:
            match = re.fullmatch(
                r'destination\.hostname == "([a-z0-9.-]+)" && destination\.port == ([0-9]+)',
                check["condition"],
            )
            assert match, f"cannot read the scanned host from {check['condition']!r}"
            scanned.add(f"{match[1]}:{match[2]}")
    chart_values = _HERE.parent / "egress-scan" / "chart" / "values.yaml"
    routes = set(yaml.safe_load(chart_values.read_text())["metrics"]["routes"])
    assert scanned == routes, (scanned, routes)


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
    # gVisor is untouched: its class default stays the only default gvisor
    # config. The fork's config is not a default (see
    # test_gvisor_fork_slice_is_wired).
    names = {d["metadata"]["name"] for d in docs if d["kind"] == "SandboxConfig"}
    assert names == {"gvisor-default", "microvm", "gvisor-fork"}


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


def test_microvm_asset_job_reaches_only_garage_and_github_releases() -> None:
    docs = _docs()
    policy = _one(docs, "CiliumNetworkPolicy", "microvm-assets-init")["spec"]
    assert policy["endpointSelector"]["matchLabels"] == {"app": "microvm-assets-init"}
    assert "ingress" not in policy
    garage, github = policy["egress"]
    assert garage["toEndpoints"] == [
        {
            "matchLabels": {
                "k8s:io.kubernetes.pod.namespace": "garage",
                "app.kubernetes.io/name": "garage",
            }
        }
    ]
    assert garage["toPorts"] == [{"ports": [{"port": "3900", "protocol": "TCP"}]}]
    assert {f["matchName"] for f in github["toFQDNs"]} == {
        "github.com",
        "release-assets.githubusercontent.com",
        "objects.githubusercontent.com",
    }
    assert github["toPorts"] == [{"ports": [{"port": "443", "protocol": "TCP"}]}]
    pod_labels = _microvm_job(docs)["spec"]["template"]["metadata"]["labels"]
    assert pod_labels == {"app": "microvm-assets-init"}


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
    # No other pool shares the spike namespace.
    pools = [d for d in docs if d["kind"] == "WorkerPool"]
    assert [
        p["metadata"]["name"] for p in pools if p["metadata"]["namespace"] == _SPIKE_NS
    ] == ["microvm-spike"]


def test_microvm_spike_namespace_allows_only_the_router_and_the_gateway() -> None:
    # The fork pool's namespace has the same policy, so pick the spike's.
    (policy,) = [
        d["spec"]
        for d in _docs()
        if d["kind"] == "CiliumNetworkPolicy"
        and d["metadata"]["name"] == "substrate-workers"
        and d["metadata"].get("namespace") == _SPIKE_NS
    ]
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


_ROUTER_CLIENTS = [
    "spiffe://cluster.local/ns/ate-system/sa/atenet-client",
    "spiffe://cluster.local/ns/kagent/sa/kagent-controller",
    # The fork Jobs and later the fork runner (Joe, 2026-11-28, HOME-924).
    "spiffe://cluster.local/ns/ate-system/sa/ate-client",
]


def test_router_allows_exactly_the_three_client_identities() -> None:
    # The router checks a client by its SPIFFE ID alone, and an allowed ID can
    # reach any actor in any atespace. engine-gate left the list when Joe
    # deferred the Semgrep tier (2026-11-24). egress-scan, which parses
    # untrusted bytes, must never be in this list, nor a wildcard, and neither
    # may the deferred engine-gate ID. The rendered ConfigMap carries
    # the list values/substrate.yaml sets.
    config = yaml.safe_load(
        _one(_docs(), "ConfigMap", "atenet-router-agentgateway-config")["data"][
            "config.yaml"
        ]
    )
    (rule,) = config["frontendPolicies"]["networkAuthorization"]["rules"]
    ids = re.search(r"source\.spiffeId in \[(.*?)\]", rule["allow"]).group(1)
    assert re.findall(r'"([^"]+)"', ids) == _ROUTER_CLIENTS
    assert "egress-scan" not in rule["allow"] and "*" not in rule["allow"]
    assert "engine-gate" not in rule["allow"]


def test_engine_gate_is_not_a_global_owner() -> None:
    # The gate gets the editor role of one atespace, not a bootstrap owner seat.
    args = [
        a
        for d in _docs()
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "ate-api-server"
        for c in d["spec"]["template"]["spec"]["containers"]
        for a in c.get("args", [])
    ]
    owners = [a for a in args if a.startswith("--authz-bootstrap-owners=")]
    assert owners == [_BOOTSTRAP_OWNERS]
    assert "engine-gate" not in _BOOTSTRAP_OWNERS


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


_FORK_NS = "substrate-gvisor-fork"
# Every object ../gvisor-fork renders (step 5 of the enable steps). A new object
# there changes the cluster, so it must be listed here in the same change.
_FORK_OBJECTS = {
    ("CiliumNetworkPolicy", "ate-system", "ate-api-server-gvisor-fork-conformance"),
    ("CiliumNetworkPolicy", "ate-system", "gvisor-fork-assets-init"),
    ("CiliumNetworkPolicy", "ate-system", "gvisor-fork-conformance"),
    ("CiliumNetworkPolicy", "ate-system", "ate-api-server-gvisor-fork-x5"),
    ("CiliumNetworkPolicy", "ate-system", "gvisor-fork-x5"),
    ("Job", "ate-system", "gvisor-fork-x5-3"),
    ("CiliumNetworkPolicy", _FORK_NS, "substrate-workers"),
    ("Job", "ate-system", "gvisor-fork-assets-init"),
    ("Job", "ate-system", "gvisor-fork-conformance-7"),
    ("Namespace", None, _FORK_NS),
    ("SandboxConfig", None, "gvisor-fork"),
    ("ServiceAccount", _FORK_NS, "fork-worker"),
    ("WorkerPool", _FORK_NS, "gvisor-fork"),
}
_GVISOR_DEFAULT = "sandboxconfig.ate.dev/is-class-default"


def test_gvisor_fork_slice_is_wired() -> None:
    # The gVisor sandbox-fork slice lives in ../gvisor-fork. ArgoCD reads only
    # this directory, and since step 5 of the enable steps it lists the slice.
    # The slice runs no factory actor: the pool has one worker (step 6), the
    # ForkSeed gate is on (step 7: test_fork_seed_gate_is_on) and the
    # conformance Job runs once (step 8). The latency Jobs stay out of the render:
    # they need a pool restart first, and the data Job a router grant.
    app = yaml.safe_load(Path(os.environ["SCANNER_APPLICATION"]).read_text())
    paths = [s["path"] for s in app["spec"]["sources"] if "path" in s]
    assert paths == ["projects/platform-factory/substrate/resources"]
    kustomization = yaml.safe_load((_HERE / "kustomization.yaml").read_text())
    listed = kustomization["resources"] + kustomization.get("components", [])
    assert [r for r in listed if r.startswith("..")] == [
        "../gvisor-fork",
    ]
    docs = _docs()

    def key(d: dict) -> tuple:
        return (d["kind"], d["metadata"].get("namespace"), d["metadata"]["name"])

    # Build the slice's own directory, so an object without "gvisor-fork" in
    # its name cannot slip past: every object it renders is listed here, and
    # ../resources renders each of them.
    slice_docs = subprocess.run(
        [os.environ["KUSTOMIZE_BIN"], "build", str(_HERE.parent / "gvisor-fork")],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    rendered = {key(d) for d in yaml.safe_load_all(slice_docs) if d}
    assert rendered <= {key(d) for d in docs}
    # The three generated ConfigMaps carry a content hash in their names.
    configmaps = {k for k in rendered if k[0] == "ConfigMap"}
    assert {k[2].rsplit("-", 1)[0] for k in configmaps} == {
        "gvisor-fork-assets",
        "gvisor-fork-conformance",
        "gvisor-fork-x5",
    }
    assert rendered - configmaps == _FORK_OBJECTS
    classes = {
        d["metadata"]["name"]: d["spec"]["sandboxClass"]
        for d in docs
        if d["kind"] == "SandboxConfig"
    }
    assert classes == {
        "gvisor-default": "gvisor",
        "gvisor-fork": "gvisor",
        "microvm": "microvm",
    }
    # gvisor-default stays the class default, so existing pools resolve it.
    configs = {d["metadata"]["name"]: d for d in docs if d["kind"] == "SandboxConfig"}
    assert configs["gvisor-default"]["metadata"]["annotations"][_GVISOR_DEFAULT]
    assert _GVISOR_DEFAULT not in configs["gvisor-fork"]["metadata"].get(
        "annotations", {}
    )
    # One worker. No template sets fork_config until the conformance Job makes
    # its own (and deletes it), so the worker holds no seed.
    pool = _one(docs, "WorkerPool", "gvisor-fork")
    assert pool["spec"]["replicas"] == 1
    conformance = _one(docs, "Job", "gvisor-fork-conformance-7")
    assert conformance["spec"]["suspend"] is False
    # A Pending Job leaves the Application Degraded and the cluster is request
    # tight, so the pod requests little.
    (container,) = conformance["spec"]["template"]["spec"]["containers"]
    assert container["resources"]["requests"]["memory"] == "64Mi"
    # The Job's config volume names the generated ConfigMap, whose name carries
    # a hash of conformance/. The live Job's pod template is immutable, so a
    # changed hash makes the apply fail and the Job never starts. Editing
    # conformance/ means a new run suffix: change this name and the Job's together.
    (conformance_map,) = [
        k[2] for k in configmaps if k[2].startswith("gvisor-fork-conformance-")
    ]
    (config_volume,) = [
        v
        for v in conformance["spec"]["template"]["spec"]["volumes"]
        if v["name"] == "config"
    ]
    assert config_volume["configMap"]["name"] == conformance_map
    assert conformance_map == "gvisor-fork-conformance-khtmk9cc87"
    assert not [
        d for d in docs if d["metadata"]["name"].startswith("gvisor-fork-latency")
    ]
    # The namespace is kept when the Application goes, like every Namespace.
    namespace = _one(docs, "Namespace", _FORK_NS)
    assert (
        "Prune=false"
        in namespace["metadata"]["annotations"]["argocd.argoproj.io/sync-options"]
    )
    # The asset Job is a Sync hook, so Argo runs it on the first sync and holds
    # that sync until its pod is scheduled: keep its pod request small
    # (the nodes are request-tight; node-2 had 1981Mi left on 2026-11-23).
    job = _one(docs, "Job", "gvisor-fork-assets-init")
    assert job["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "Sync"
    pod = job["spec"]["template"]["spec"]
    init = max(
        int(c["resources"]["requests"]["memory"].removesuffix("Mi"))
        for c in pod["initContainers"]
    )
    main = sum(
        int(c["resources"]["requests"]["memory"].removesuffix("Mi"))
        for c in pod["containers"]
    )
    assert max(init, main) <= 256


def test_engine_provisioning_is_not_wired() -> None:
    # Joe deferred the Semgrep tier on 2026-11-24. The provisioning and teardown
    # Jobs (egress-scan/gate/provision) ran their course and are not in the
    # kustomization: the wired render holds none of their objects.
    names = {(d["kind"], d["metadata"]["name"]) for d in _docs()}
    assert not {n for k, n in names if k == "Job" and n.startswith("egress-engine-")}
    assert not {n for k, n in names if n.startswith("egress-engine-provision")}


_GATE_IMAGE = (
    "ghcr.io/jomcgi/homelab/projects/platform-factory/substrate/egress-scan/gate"
)
# Every object ../egress-scan/gate/manifests renders. A new object there changes
# the cluster, so it must be listed here in the same change.
_GATE_OBJECTS = {
    ("CiliumClusterwideNetworkPolicy", None, "prometheus-scrape-engine-gate"),
    ("CiliumNetworkPolicy", "ate-system", "ate-api-server-engine-gate"),
    ("CiliumNetworkPolicy", "ate-system", "egress-scan-to-engine-gate"),
    ("CiliumNetworkPolicy", "ate-system", "engine-gate"),
    ("Service", "ate-system", "engine-gate"),
    ("ServiceAccount", "ate-system", "engine-gate"),
    ("StatefulSet", "ate-system", "engine-gate"),
    ("ValidatingAdmissionPolicy", None, "engine-gate-pods"),
    ("ValidatingAdmissionPolicy", None, "engine-gate-serviceaccount"),
    ("ValidatingAdmissionPolicy", None, "engine-gate-workloads"),
    ("ValidatingAdmissionPolicyBinding", None, "engine-gate-pods"),
    ("ValidatingAdmissionPolicyBinding", None, "engine-gate-serviceaccount"),
    ("ValidatingAdmissionPolicyBinding", None, "engine-gate-workloads"),
}


def test_engine_gate_is_not_deployed_and_its_pin_follows_the_build() -> None:
    # Joe deferred the Semgrep tier on 2026-11-24. The gate's manifests stay in
    # the repository (egress-scan/gate/manifests) and are not listed in
    # kustomization.yaml, so the wired render holds none of its objects.
    docs = _docs()

    def key(d: dict) -> tuple:
        return (d["kind"], d["metadata"].get("namespace"), d["metadata"]["name"])

    wired = {key(d) for d in docs}
    # The admission guard of the ServiceAccount left with the router grant: no
    # ID of the gate is trusted any more (test_router_allows_exactly_the_two_...).
    assert not (_GATE_OBJECTS & wired), "engine-gate objects are wired"
    # The gate's own directory still renders exactly _GATE_OBJECTS, so that
    # deploying it again is a one-line change that this test already covers.
    gate_docs = subprocess.run(
        [
            os.environ["KUSTOMIZE_BIN"],
            "build",
            str(_HERE.parent / "egress-scan" / "gate" / "manifests"),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    rendered = [d for d in yaml.safe_load_all(gate_docs) if d]
    assert {key(d) for d in rendered} == _GATE_OBJECTS
    sts = _one(rendered, "StatefulSet", "engine-gate")
    (container,) = sts["spec"]["template"]["spec"]["containers"]
    digest = Path(os.environ["GATE_DIGEST"]).read_text().strip()
    # The pin is the digest of the image that main's CI pushed: a change to the
    # gate's code or its image config changes it, and the pin follows in the
    # same change (egress-scan/gate/manifests/kustomization.yaml).
    assert container["image"] == f"{_GATE_IMAGE}@{digest}"


_PREVIEW = ["PerTemplateSnapshotLocation", "GoldenEgressPolicy", "ForkSeed"]


def test_fork_seed_gate_is_on() -> None:
    # Step 7 of the enable steps (projects/factory-cluster/README.md, "gVisor
    # sandbox fork"): the API server, the controller and atelet all take
    # ForkSeed in --preview. The controller passes its --preview to worker
    # pods. An upstream v1.0.1 worker does not know ForkSeed and exits on it,
    # so the control plane must be the fork build that gives the gate to
    # workers of KVM pools only (substrate 1e7a877d0ca3, test
    # TestBuildDeploymentPreviewForkSeed: "non-KVM args equal gate-off
    # args"). This test cannot run the controller. It pins that build and
    # checks that the gate is only ever in the three components' --preview
    # and that no non-KVM pool names a worker image that knows the gate. The
    # WorkerPool CRD's field text names the gate, so only container arguments
    # are read.
    previews = {}
    for doc in _docs():
        if doc["kind"] not in {"Deployment", "DaemonSet", "StatefulSet"}:
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            for arg in container.get("args", []):
                if arg.startswith("--preview"):
                    previews[doc["metadata"]["name"]] = arg
    expected = "--preview=" + ",".join(_PREVIEW)
    assert previews == {
        "ate-api-server": expected,
        "ate-controller": expected,
        "atelet": expected,
    }
    # Only the gvisor-fork pool can hold a seed: no other pool runs on the KVM
    # platform.
    kvm_pools = [
        d["metadata"]["name"]
        for d in _docs()
        if d["kind"] == "WorkerPool"
        and any(c.get("platform") == "kvm" for c in d["spec"]["sandboxClasses"])
    ]
    assert kvm_pools == ["gvisor-fork"]
    # ForkSeed is nowhere else: no other container argument or variable of a
    # rendered workload names it.
    for doc in _docs():
        if doc["kind"] not in {"Deployment", "DaemonSet", "StatefulSet", "Job"}:
            continue
        pod = doc["spec"]["template"]["spec"]
        for container in pod.get("initContainers", []) + pod["containers"]:
            texts = [*container.get("args", []), *container.get("command", [])]
            texts += [e.get("value", "") for e in container.get("env", [])]
            named = [x for x in texts if "ForkSeed" in x]
            if doc["metadata"]["name"] in previews:
                named = [x for x in named if not x.startswith("--preview=")]
            assert not named, (doc["metadata"]["name"], named)
    # The control plane is a build that gives the gate to KVM pools' workers
    # only: add a later build here when the fork controller changes.
    controller = _images(_docs())["ate-controller/ate-controller"]
    assert controller.split("@")[0].rsplit(":", 1)[1] in _KVM_ONLY_GATE_BUILDS
    # Every other pool in this render (the repository's other WorkerPools are
    # in the factory chart) runs an upstream v1.0.1 worker, which
    # does not know ForkSeed. Its --preview must stay as it is with the gate
    # off, which only the build above guarantees.
    for doc in _docs():
        if doc["kind"] == "WorkerPool" and doc["metadata"]["name"] != "gvisor-fork":
            image = doc["spec"]["workerImage"]
            assert image.startswith("ghcr.io/kagent-dev/substrate/ateworker-"), image
            assert ":v1.0.1@sha256:" in image, image


# 8cf248b96e08 contains the earlier KVM-only ForkSeed patch (1e7a877d0ca3), so
# both builds give the gate to KVM pools' workers only.
_KVM_ONLY_GATE_BUILDS = {"fork-1e7a877d0ca3", "fork-8cf248b96e08", "fork-c7f37a993862"}
_FORK_IMAGES = "ghcr.io/jomcgi-org/homelab/substrate-fork/"
_FORK_TAG = "fork-c7f37a993862"
_FORK_DIGESTS = {
    "ateapi": "c8389dcf2f0d5229db59125a9a5a0d26cc284783b9a6dad772901471d8018a5b",
    "atecontroller": "3750e645a59ba980bb3b4c502940f267060cb37157b980bca0eb69ca809992d1",
    "atelet": "da40d52d3d83164d706c9278fb6cda8b9b3ef3bdb652946b3451a0541b39a736",
}


def _images(docs: list[dict]) -> dict[str, str]:
    """Every container image in the rendered Deployments, DaemonSets and
    StatefulSets, keyed by workload and container name."""
    out = {}
    for doc in docs:
        if doc["kind"] not in {"Deployment", "DaemonSet", "StatefulSet"}:
            continue
        pod = doc["spec"]["template"]["spec"]
        for container in pod.get("initContainers", []) + pod["containers"]:
            key = f"{doc['metadata']['name']}/{container['name']}"
            out[key] = container["image"]
    return out


def test_control_plane_and_atelet_run_the_fork_images_by_digest() -> None:
    images = _images(_docs())
    expected = {
        "ate-api-server/ate-api-server": "ateapi",
        "ate-controller/ate-controller": "atecontroller",
        "atelet/atelet": "atelet",
    }
    for key, component in expected.items():
        assert images[key] == (
            f"{_FORK_IMAGES}{component}:{_FORK_TAG}@sha256:{_FORK_DIGESTS[component]}"
        ), key
    # Nothing else moved off the v1.0.1 release.
    forked = {k for k, v in images.items() if v.startswith(_FORK_IMAGES)}
    assert forked == set(expected)
    for key, image in images.items():
        if key not in expected:
            assert "@sha256:" in image, key


def test_fork_images_share_one_fork_commit_tag() -> None:
    # The control plane, the pool's worker and the conformance Job's kubectl-ate
    # are built from one fork commit. A worker and a controller from different
    # commits can disagree on the preview gates or the fork wire (the
    # controller passes --preview to the worker).
    docs = _docs()
    images = _images(docs)
    refs = {
        key: images[key]
        for key in (
            "ate-api-server/ate-api-server",
            "ate-controller/ate-controller",
            "atelet/atelet",
        )
    }
    refs["workerpool/gvisor-fork"] = _one(docs, "WorkerPool", "gvisor-fork")["spec"][
        "workerImage"
    ]
    job = _one(docs, "Job", "gvisor-fork-conformance-7")
    (cli,) = [
        v["image"]["reference"]
        for v in job["spec"]["template"]["spec"]["volumes"]
        if v["name"] == "cli"
    ]
    refs["conformance/cli"] = cli
    tags = {key: ref.split("@")[0].rsplit(":", 1)[1] for key, ref in refs.items()}
    assert set(tags.values()) == {_FORK_TAG}, tags
    for key, ref in refs.items():
        assert ref.startswith(_FORK_IMAGES), key
        assert re.search(r"@sha256:[0-9a-f]{64}$", ref), key


def test_workerpool_crd_has_the_fork_platform_field() -> None:
    crd = _one(
        _docs(),
        "CustomResourceDefinition",
        "workerpools.ate.dev",
    )
    (version,) = crd["spec"]["versions"]
    item = version["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"][
        "sandboxClasses"
    ]["items"]
    assert item["properties"]["platform"]["enum"] == ["systrap", "kvm"]
    (rule,) = item["x-kubernetes-validations"]
    assert rule["rule"] == "!has(self.platform) || self.name == 'gvisor'"
    # Existing pools set no platform, so the new rule changes none of them.
    assert "platform" not in item["required"]


# The gateway gives the whole check requestTimeout. The scanner gives its one
# detector `detector.deadline` (75ms), so the timeout is the deadline plus a
# margin: shorter and the gateway cancels a check that the scanner would still
# answer. The second scan tier (Semgrep) left on 2026-11-24 (egress-scan/
# ARCHITECTURE.md "Decided" 9 and 10).
_GATEWAY_MARGIN_MS = 5
_GATEWAY_TIMEOUT = "80ms"
_DETECTORS = {"gitleaks", "betterleaks"}


def _ms(duration: str) -> int:
    match = re.fullmatch(r"([0-9]+)(ms|s)", duration)
    assert match, f"use a whole number of ms or s, got {duration!r}"
    return int(match[1]) * (1000 if match[2] == "s" else 1)


def _detector_values() -> dict:
    """The chart defaults overlaid with the values the Application passes."""
    chart_values = _HERE.parent / "egress-scan" / "chart" / "values.yaml"
    values = yaml.safe_load(chart_values.read_text())["detector"]
    app = yaml.safe_load(Path(os.environ["SCANNER_APPLICATION"]).read_text())
    helm = app["spec"]["sources"][0]["helm"]
    assert not [
        p for p in helm.get("parameters", []) if p["name"].startswith("detector.")
    ], "set detector values in helm.valuesObject so that this test reads them"
    values.update(helm.get("valuesObject", {}).get("detector", {}))
    return values


def _check_gateway_covers_detector(deadline: str, gateway_timeout: str) -> None:
    needed = _ms(deadline) + _GATEWAY_MARGIN_MS
    assert _ms(gateway_timeout) >= needed, (
        f"requestTimeout {gateway_timeout} is below detector.deadline {deadline}"
        f" + {_GATEWAY_MARGIN_MS}ms; the gateway would cancel the check first"
    )


def test_gateway_timeout_covers_the_detector_deadline() -> None:
    policy = yaml.safe_load((_HERE / "egress-scan" / "gateway-policy.yaml").read_text())
    (check,) = policy["conditional"]
    timeout = check["policies"]["http"]["requestTimeout"]
    assert timeout == _GATEWAY_TIMEOUT
    _check_gateway_covers_detector(_detector_values()["deadline"], timeout)


def test_the_coupling_check_fails_when_the_gateway_is_too_short() -> None:
    _check_gateway_covers_detector("75ms", "80ms")
    _check_gateway_covers_detector("300ms", "310ms")
    for deadline, timeout in (("75ms", "79ms"), ("1s", "80ms")):
        try:
            _check_gateway_covers_detector(deadline, timeout)
        except AssertionError:
            continue
        raise AssertionError(f"accepted {deadline} {timeout}")


def test_the_application_names_its_detector_explicitly() -> None:
    # A typo in the key would leave the chart default without a failure.
    app = yaml.safe_load(Path(os.environ["SCANNER_APPLICATION"]).read_text())
    given = app["spec"]["sources"][0]["helm"]["valuesObject"]
    assert set(given) == {"detector"} and set(given["detector"]) == {"name"}
    assert given["detector"]["name"] in _DETECTORS


def test_the_scanner_runs_one_known_detector_in_enforce_mode() -> None:
    values = _detector_values()
    assert values["name"] in _DETECTORS
    assert values["mode"] == "enforce"
    assert values["failureMode"] == "deny"
    assert values["maxInputBytes"] <= 512 * 1024
    assert not values["dataPath"]


def test_the_application_sets_no_semgrep_values() -> None:
    # The chart has no Semgrep values any more (egress-scan/ARCHITECTURE.md
    # "Decided" 10). The Application must not set them.
    app = yaml.safe_load(Path(os.environ["SCANNER_APPLICATION"]).read_text())
    assert "semgrep" not in str(app["spec"]["sources"][0]["helm"])
