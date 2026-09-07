"""The dev deployment must not claim any identity production owns.

Dev loads ONLY its own values file, so chart defaults win wherever dev is
silent. That is the difference from the monolith: inheriting is safe for
settings describing how a workload BEHAVES, and unsafe for the ones describing
WHO IT CLAIMS TO BE. For embervm, the hazard is chart defaults leaking in
when dev's values go silent.

Three isolation failures got through code review and were caught only by a
human rendering both environments and diffing them by hand:

  1. Dev's Workload CRs collided with production's by name. The control plane
     listed the cluster-wide collection and keyed its catalog on name alone,
     so each control plane patched `.status` onto the other's CR. This is now
     fixed by scoping the informer to namespace, but shared names are still a
     latent hazard for future drift.

  2. Dev re-enabled the `noded` DaemonSet and privileged `scratch-prep` on
     all four nodes including the three etcd masters, because both default
     true in the chart and only production's values disable them.

  3. `rootfsPath` was not overridden, so base builds targeted production's
     scratch path.

Neither is reachable by a normal unit test, and neither shows up as a red
render: both charts template perfectly. Only comparing the two rendered
outputs against each other finds them, which is what this does.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

# Kinds with no namespace: two Applications rendering the same name means one
# object with two owners, not two objects.
CLUSTER_SCOPED = {
    "ClusterRole",
    "ClusterRoleBinding",
    "CustomResourceDefinition",
    "PriorityClass",
    "StorageClass",
    "ValidatingWebhookConfiguration",
    "MutatingWebhookConfiguration",
}

_DOC_KIND = re.compile(r"^kind:\s*(\S+)\s*$", re.M)
_DOC_NAME = re.compile(r"^\s{2}name:\s*(\S+)\s*$", re.M)


def _chart_dir() -> Path:
    here = Path(__file__).resolve().parent
    if (here / "Chart.yaml").exists():
        return here
    raise RuntimeError("Could not find chart Chart.yaml")


def _kernel_boot_args(values_path: Path) -> str:
    matches = re.findall(
        r'^\s{4}kernelBootArgs:\s*"([^"]*)"\s*$',
        values_path.read_text(),
        re.MULTILINE,
    )
    assert len(matches) == 1, (
        f"expected one noded.firecracker.kernelBootArgs in {values_path}, "
        f"found {len(matches)}"
    )
    return matches[0]


def _decode_kernel_env(args: str) -> tuple[str, dict[str, str]]:
    boot_args = []
    env = {}
    for token in args.split():
        if not token.startswith("ember.env."):
            boot_args.append(token)
            continue
        key, encoded = token.removeprefix("ember.env.").split("=", 1)
        assert key not in env, f"duplicate guest environment token: {key}"
        env[key] = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode(
            "utf-8"
        )
    return " ".join(boot_args), env


_PROGRESS_URL = "http://monolith.monolith.svc.cluster.local:8091/ingest/progress"
_AGENT_MCP_URL = (
    "http://monolith-agents-agents.monolith-agents.svc.cluster.local:8092/mcp"
)
_KERNEL_ENV_CASES = [
    pytest.param([], {}, id="defaults"),
    pytest.param(["PROD_VALUES"], {"EMBER_PROGRESS_URL": _PROGRESS_URL}, id="home"),
    pytest.param(
        ["PROD_VALUES", "GKE_VALUES"],
        {"EMBER_AGENT_MCP_URL": _AGENT_MCP_URL, "EMBER_PROGRESS_URL": _PROGRESS_URL},
        id="gke",
    ),
    pytest.param(["DEV_VALUES"], {}, id="dev"),
]


@pytest.mark.parametrize("values_names, expected_env", _KERNEL_ENV_CASES)
def test_guest_kernel_env_source(values_names, expected_env) -> None:
    default_args = _kernel_boot_args(_chart_dir() / "values.yaml")
    args = default_args
    for name in values_names:
        values = yaml.safe_load(Path(os.environ[name]).read_text())
        args = (
            values.get("noded", {}).get("firecracker", {}).get("kernelBootArgs", args)
        )
    boot_args, env = _decode_kernel_env(args)
    assert boot_args == default_args
    assert env == expected_env


@pytest.mark.parametrize("values_names, expected_env", _KERNEL_ENV_CASES)
def test_guest_kernel_env_rendered_on_every_noded(values_names, expected_env) -> None:
    rendered = _render("guest-env", [Path(os.environ[name]) for name in values_names])
    noded_args = []
    for document in yaml.safe_load_all(rendered):
        if not isinstance(document, dict):
            continue
        pod_spec = document.get("spec", {}).get("template", {}).get("spec", {})
        for container in pod_spec.get("containers", []):
            if container.get("name") == "noded":
                env = {entry["name"]: entry for entry in container.get("env", [])}
                noded_args.append(env["EMBERVM_NODED_KERNEL_BOOT_ARGS"]["value"])
    assert noded_args, "expected at least one rendered noded container"
    for args in noded_args:
        boot_args, env = _decode_kernel_env(args)
        assert boot_args == _kernel_boot_args(_chart_dir() / "values.yaml")
        assert env == expected_env


def _render(
    release: str, values: list[Path], set_values: list[str] | None = None
) -> str:
    helm_bin = os.environ.get("HELM_BIN", "helm")
    argv = [helm_bin, "template", release, str(_chart_dir()), "--namespace", release]
    for v in values:
        argv += ["--values", str(v)]
    for value in set_values or []:
        argv += ["--set", value]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return result.stdout


def _render_with_set(release: str, settings: list[str]) -> str:
    helm_bin = os.environ.get("HELM_BIN", "helm")
    argv = [helm_bin, "template", release, str(_chart_dir()), "--namespace", release]
    for setting in settings:
        argv += ["--set", setting]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return result.stdout


def _rootfs_builder_init_containers(rendered: str) -> list[dict]:
    containers = []
    for document in yaml.safe_load_all(rendered):
        if not isinstance(document, dict):
            continue
        pod_spec = document.get("spec", {}).get("template", {}).get("spec", {})
        for container in pod_spec.get("initContainers", []):
            name = container.get("name", "")
            if name.startswith("build-") and name.endswith("-rootfs"):
                containers.append(container)
    return containers


def test_store_credentials_are_default_off_and_share_one_secret() -> None:
    anonymous = _render_with_set("e", [])
    assert "STORE_ACCESS_KEY_ID" not in anonymous
    assert "onepassword.com/v1" not in "\n".join(
        doc
        for kind, _name, doc in _docs(anonymous)
        if kind == "OnePasswordItem" and "-store" in doc
    )

    signed = _render_with_set(
        "e",
        [
            "noded.store.credentials.enabled=true",
            "noded.store.credentials.onepassword.itemPath=vaults/x/items/y",
        ],
    )
    assert "EMBERVM_NODED_STORE_ACCESS_KEY_ID" in signed
    assert "EMBERVM_STORE_ACCESS_KEY_ID" in signed
    assert signed.count("name: e-embervm-store") >= 3
    assert "kind: OnePasswordItem" in signed
    assert 'itemPath: "vaults/x/items/y"' in signed


def test_rootfs_builders_receive_store_env_only_when_store_enabled() -> None:
    enabled = _render_with_set(
        "rootfs-store",
        [
            "noded.store.endpoint=https://objects.example.test",
            "noded.store.bucket=rootfs-cache",
            "noded.store.credentials.enabled=true",
            "noded.store.credentials.secretName=rootfs-store-credentials",
        ],
    )
    enabled_containers = _rootfs_builder_init_containers(enabled)
    assert enabled_containers, (
        "store-enabled render has no rootfs builder init containers"
    )
    expected_env = {
        "EMBERVM_NODED_STORE_ENDPOINT": {
            "name": "EMBERVM_NODED_STORE_ENDPOINT",
            "value": "https://objects.example.test",
        },
        "EMBERVM_NODED_STORE_BUCKET": {
            "name": "EMBERVM_NODED_STORE_BUCKET",
            "value": "rootfs-cache",
        },
        "EMBERVM_NODED_STORE_ACCESS_KEY_ID": {
            "name": "EMBERVM_NODED_STORE_ACCESS_KEY_ID",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "rootfs-store-credentials",
                    "key": "access_key_id",
                }
            },
        },
        "EMBERVM_NODED_STORE_SECRET_ACCESS_KEY": {
            "name": "EMBERVM_NODED_STORE_SECRET_ACCESS_KEY",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "rootfs-store-credentials",
                    "key": "secret_access_key",
                }
            },
        },
    }
    for container in enabled_containers:
        env = {entry["name"]: entry for entry in container.get("env", [])}
        for name, expected in expected_env.items():
            assert env.get(name) == expected

    disabled = _render_with_set(
        "rootfs-store-disabled",
        [
            "noded.store.endpoint=",
            "noded.store.credentials.enabled=true",
        ],
    )
    disabled_containers = _rootfs_builder_init_containers(disabled)
    assert disabled_containers, (
        "store-disabled render has no rootfs builder init containers"
    )
    store_env_names = set(expected_env)
    for container in disabled_containers:
        env_names = {entry["name"] for entry in container.get("env", [])}
        assert store_env_names.isdisjoint(env_names)


def test_noded_egress_catalog_renders_plaintext_upstream_opt_in(
    tmp_path: Path,
) -> None:
    overlay = tmp_path / "plaintext-upstream.yaml"
    overlay.write_text(
        """egress:
  enabled: true
  secrets:
    - header: Authorization
      brokerGrant: internal-api
      egressTo: [internal-api.default.svc]
      plaintextUpstream: true
    - header: X-Token
      env: LEGACY_TOKEN
      egressTo: [legacy.default.svc]
      secretRef: {name: legacy-token, key: token}
"""
    )
    rendered = _render(
        "plaintext-upstream",
        [_chart_dir() / "values.yaml", overlay],
    )
    noded = [
        doc
        for kind, _name, doc in _docs(rendered)
        if kind == "DaemonSet" and "- name: egress-proxy" in doc
    ]
    assert len(noded) == 1, f"expected one rendered noded pod, got {len(noded)}"
    match = re.search(
        r'^\s*- name: EGRESS_SECRETS\s*\n\s+value: ("(?:\\.|[^"\\])*")\s*$',
        noded[0],
        re.MULTILINE,
    )
    assert match, "noded egress-proxy has no EGRESS_SECRETS env value"
    catalog = json.loads(json.loads(match.group(1)))
    assert catalog[0]["plaintextUpstream"] is True
    assert catalog[1]["plaintextUpstream"] is False


def _docs(rendered: str):
    for doc in rendered.split("\n---"):
        kind = _DOC_KIND.search(doc)
        name = _DOC_NAME.search(doc)
        if kind and name:
            yield kind.group(1), name.group(1), doc


def _application_name(application_yaml: Path) -> str:
    """Read the Application's metadata.name to infer the release name.

    EmberVM Applications don't use a separate releaseName field; the
    Application's name IS the release name (embervm or embervm-dev).
    """
    content = application_yaml.read_text()
    # Skip the namespace line and find the first name after metadata.
    in_metadata = False
    for line in content.split("\n"):
        if "metadata:" in line:
            in_metadata = True
        if in_metadata:
            match = _APP_NAME.search(line)
            if match:
                return match.group(1)
    raise RuntimeError(f"Could not find Application name in {application_yaml}")


_APP_NAME = re.compile(r"^\s{2}name:\s*(\S+)\s*$", re.M)

# Module-level, used by the isolation assertions at the bottom.
# ANCHORED on the hostPath key, not any `path:`. An unanchored version matched
# HTTP probe paths (`path: /healthz`) and reported /healthz as a shared hostPath,
# which is a false positive of exactly the kind that drives an override rate:
# the assertion fires, nobody can act on it, and the next person widens it.
_HOSTPATH = re.compile(r"hostPath:\s*\n\s+path: (/\S+)", re.M)
_ROOTFS_PATH = re.compile(r"BASE_ROOTFS_PATH\"?\s*\n\s*value: \"?(/\S+?)\"?\s*$", re.M)


@pytest.fixture(scope="module")
def renders():
    chart = _chart_dir()
    prod_values = Path(os.environ["PROD_VALUES"])
    dev_values = Path(os.environ["DEV_VALUES"])
    prod_release = _application_name(Path(os.environ["PROD_APPLICATION"]))
    dev_release = _application_name(Path(os.environ["DEV_APPLICATION"]))
    return {
        "prod": _render(prod_release, [chart / "values.yaml", prod_values]),
        "dev": _render(dev_release, [chart / "values.yaml", dev_values]),
    }


def test_renders_are_non_empty(renders):
    """Guard the guard: both renders must be non-empty before we compare them.

    A vacuous test that passes on empty renders is worse than no test, because
    it silently reports all assertions as passing while proving nothing.
    """
    assert renders["prod"], "production render is empty; this test is inert"
    assert renders["dev"], "dev render is empty; this test is inert"
    assert renders["prod"].count("kind:") > 20, (
        "production render has suspiciously few documents; this test may be inert"
    )
    assert renders["dev"].count("kind:") > 5, (
        "dev render has suspiciously few documents; this test may be inert"
    )


def test_conformance_runner_is_dev_only(renders):
    prod = [
        (kind, name)
        for kind, name, _doc in _docs(renders["prod"])
        if "conformance" in name
    ]
    dev = [
        (kind, name)
        for kind, name, _doc in _docs(renders["dev"])
        if "conformance" in name
    ]
    assert prod == []
    assert dev == [
        ("ServiceAccount", "embervm-dev-embervm-conformance"),
        ("Service", "embervm-dev-embervm-conformance"),
        ("Deployment", "embervm-dev-embervm-conformance"),
    ]
    assert (
        "system:serviceaccount:embervm-dev:embervm-dev-embervm-conformance"
        in renders["dev"]
    )


def test_conformance_runner_refuses_production_namespace():
    helm_bin = os.environ.get("HELM_BIN", "helm")
    result = subprocess.run(
        [
            helm_bin,
            "template",
            "embervm",
            str(_chart_dir()),
            "--namespace",
            "embervm",
            "--set",
            "conformance.enabled=true",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "conformance runner is dev-only" in result.stderr


def test_control_plane_runtime_envs_render(renders):
    def control_plane_env(rendered: str) -> dict[str, str]:
        deployments = [
            doc
            for kind, _, doc in _docs(rendered)
            if kind == "Deployment"
            and re.search(r"^\s+- name: control-plane\s*$", doc, re.M)
        ]
        assert len(deployments) == 1, (
            f"expected one control-plane Deployment, found {len(deployments)}"
        )
        return dict(
            re.findall(
                r'^\s+- name: (EMBERVM_[A-Z0-9_]+)\s*\n\s+value: "([^"]*)"\s*$',
                deployments[0],
                re.M,
            )
        )

    prod_env = control_plane_env(renders["prod"])
    dev_env = control_plane_env(renders["dev"])

    assert "EMBERVM_BRICK_AUTOSCALE_MODE" in prod_env
    assert dev_env.get("EMBERVM_BRICK_AUTOSCALE_MODE") == "observe"
    assert "EMBERVM_WARMTH_S3_GC_EXPECTED_NODES" not in dev_env
    assert prod_env.get("EMBERVM_ENVELOPE_REWRAP_ENABLED") == "0"
    assert dev_env.get("EMBERVM_ENVELOPE_REWRAP_ENABLED") == "0"
    assert prod_env.get("EMBERVM_ENVELOPE_REWRAP_MAX_ARTIFACTS") == "100"
    assert prod_env.get("EMBERVM_ENVELOPE_REWRAP_CONCURRENCY") == "8"
    assert prod_env.get("EMBERVM_ENVELOPE_REWRAP_INTERVAL_MS") == "3600000"
    assert prod_env.get("EMBERVM_SESSION_INVOKE_WATCHDOG_MARGIN_MS") == "15000"
    assert dev_env.get("EMBERVM_SESSION_INVOKE_WATCHDOG_MARGIN_MS") == "15000"


def test_noded_bearer_secret_flips_control_plane_and_bricks_together():
    chart = _chart_dir()
    enabled = _render(
        "noded-auth",
        [chart / "values.yaml"],
        [
            "bricks.enabled=true",
            "noded.bearerTokenSecret.enabled=true",
            "noded.bearerTokenSecret.name=x",
        ],
    )

    deployments = {
        name: doc for kind, name, doc in _docs(enabled) if kind == "Deployment"
    }
    control_plane = deployments["noded-auth-embervm"]
    bricks = [doc for name, doc in deployments.items() if "-noded-brick-" in name]
    assert bricks, "bearer render produced no brick Deployment; this test is inert"

    secret_env = re.compile(
        r"name:\s*EMBERVM_NODED_BEARER_TOKEN\s+valueFrom:\s+"
        r"secretKeyRef:\s+name:\s*x\s+key:\s*token",
        re.S,
    )
    assert secret_env.search(control_plane), (
        "enabled bearer auth did not render secret x on the control-plane Deployment"
    )
    assert all(secret_env.search(brick) for brick in bricks), (
        "enabled bearer auth did not render secret x on every brick Deployment"
    )

    disabled = _render(
        "noded-auth",
        [chart / "values.yaml"],
        ["bricks.enabled=true", "noded.bearerTokenSecret.enabled=false"],
    )
    assert "EMBERVM_NODED_BEARER_TOKEN" not in disabled


def test_noded_admission_model_defaults_observed_and_accepts_reserved():
    chart = _chart_dir()

    cases = [
        (
            ["bricks.enabled=true"],
            {
                "EMBERVM_NODED_ADMISSION_MODEL": "observed",
                "EMBERVM_NODED_VM_OVERHEAD_MIB": "0",
            },
        ),
        (
            [
                "bricks.enabled=true",
                "noded.admissionModel=reserved",
                "noded.vmOverheadMib=512",
            ],
            {
                "EMBERVM_NODED_ADMISSION_MODEL": "reserved",
                "EMBERVM_NODED_VM_OVERHEAD_MIB": "512",
            },
        ),
    ]

    for values, expected in cases:
        rendered = _render("noded-admission", [chart / "values.yaml"], values)
        brick_deployments = [
            doc
            for kind, _, doc in _docs(rendered)
            if kind == "Deployment"
            and "app.kubernetes.io/component: noded-brick" in doc
        ]
        assert brick_deployments, (
            "noded admission render produced no brick Deployment; this test is inert"
        )

        for deployment in brick_deployments:
            for name, value in expected.items():
                rendered_env = re.search(
                    rf'name:\s*{name}\s+value:\s*"([^\"]+)"', deployment
                )
                assert rendered_env, f"brick Deployment is missing {name}"
                assert rendered_env.group(1) == value, (
                    f"brick Deployment renders {name}={rendered_env.group(1)!r}, "
                    f"want {value!r}"
                )


def test_noded_max_live_vms_accepts_per_class_override(tmp_path: Path):
    chart = _chart_dir()
    fleet_value = "11"

    def max_live_vms(document: str) -> str:
        rendered_env = re.search(
            r'name:\s*EMBERVM_NODED_MAX_LIVE_VMS\s+value:\s*"([^\"]+)"',
            document,
        )
        assert rendered_env, "noded pod is missing EMBERVM_NODED_MAX_LIVE_VMS"
        return rendered_env.group(1)

    default_render = _render(
        "noded-max-live-default",
        [chart / "values.yaml"],
        ["bricks.enabled=true", f"noded.maxLiveVMs={fleet_value}"],
    )
    default_bricks = [
        doc
        for kind, _, doc in _docs(default_render)
        if kind == "Deployment" and "app.kubernetes.io/component: noded-brick" in doc
    ]
    assert default_bricks, (
        "default max-live render produced no brick Deployment; this test is inert"
    )
    assert all(max_live_vms(brick) == fleet_value for brick in default_bricks)

    override = tmp_path / "per-class-max-live.yaml"
    override.write_text(
        """bricks:
  enabled: true
  classes:
    - name: small
      maxLiveVMs: 3
      resources:
        requests:
          cpu: "1"
          memory: 2Gi
        limits:
          memory: 2Gi
    - name: large
      resources:
        requests:
          cpu: "1"
          memory: 4Gi
        limits:
          memory: 4Gi
    - name: open
      maxLiveVMs: 0
      resources:
        requests:
          cpu: "1"
          memory: 8Gi
        limits:
          memory: 8Gi
  nodeFloors:
    - node: test-node
      class: small
"""
    )
    override_render = _render(
        "noded-max-live-override",
        [chart / "values.yaml", override],
        [f"noded.maxLiveVMs={fleet_value}"],
    )
    override_bricks = {
        name: doc
        for kind, name, doc in _docs(override_render)
        if kind == "Deployment" and "app.kubernetes.io/component: noded-brick" in doc
    }
    assert len(override_bricks) == 4
    for name, deployment in override_bricks.items():
        if "-brick-small" in name:
            expected = "3"
        elif "-brick-open" in name:
            # An explicit 0 must survive: noded reads it as no node-side ceiling.
            expected = "0"
        else:
            expected = fleet_value
        assert max_live_vms(deployment) == expected

    for rendered in (default_render, override_render):
        wildcard = [
            doc
            for kind, name, doc in _docs(rendered)
            if kind == "DaemonSet" and name.endswith("-embervm-noded")
        ]
        assert len(wildcard) == 1
        assert max_live_vms(wildcard[0]) == fleet_value


def test_noded_onepassword_item_uses_default_shared_secret_name():
    chart = _chart_dir()
    rendered = _render(
        "noded-auth",
        [chart / "values.yaml"],
        [
            "bricks.enabled=true",
            "noded.bearerTokenSecret.enabled=true",
            "noded.bearerTokenSecret.onepassword.itemPath=vaults/x/items/y",
        ],
    )

    item_docs = [
        doc
        for kind, name, doc in _docs(rendered)
        if kind == "OnePasswordItem" and name.endswith("-noded-token")
    ]
    assert len(item_docs) == 1
    assert "name: noded-auth-embervm-noded-token" in item_docs[0]

    token_secret_refs = re.findall(
        r"name:\s*EMBERVM_NODED_BEARER_TOKEN\s+valueFrom:\s+"
        r"secretKeyRef:\s+name:\s*(\S+)",
        rendered,
        re.S,
    )
    assert token_secret_refs
    assert set(token_secret_refs) == {"noded-auth-embervm-noded-token"}


def test_noded_network_policy_gate_and_listener_ports():
    chart = _chart_dir()
    enabled = _render(
        "noded-policy",
        [chart / "values.yaml"],
        ["noded.networkPolicy.enabled=true"],
    )
    policies = [
        (name, doc)
        for kind, name, doc in _docs(enabled)
        if kind == "CiliumNetworkPolicy" and name.endswith("-noded")
    ]
    assert len(policies) == 1
    _, policy = policies[0]
    for port in ("8080", "8081", "9090", "30002"):
        assert f'port: "{port}"' in policy
    # The serving DNAT range is one endPort entry, never an enumerated list:
    # the Cilium CRD caps toPorts.ports at 40 items.
    assert "endPort: 30254" in policy
    assert 'port: "30254"' not in policy
    # noded binds the stateful and composite activator ranges by default even
    # though the chart renders no env for them; omitting them dropped the
    # stateful wake (2026-08-22, #4693).
    for start, end in (("5400", 5409), ("5410", 5419)):
        assert f'port: "{start}"' in policy
        assert f"endPort: {end}" in policy

    disabled = _render(
        "noded-policy",
        [chart / "values.yaml"],
        ["noded.networkPolicy.enabled=false"],
    )
    assert not [
        name
        for kind, name, _ in _docs(disabled)
        if kind == "CiliumNetworkPolicy" and name.endswith("-noded")
    ]


def test_dev_claims_no_cluster_scoped_object_production_owns(renders):
    """Cluster-scoped objects with shared names belong to two owners at once.

    Cluster-scoped resource kinds (ClusterRole, ClusterRoleBinding) have no
    namespace, so two Applications rendering the same name means one object
    with two ArgoCD owners, both with selfHeal. ArgoCD's shared-resource
    protection catches this and errors. Failure 1 was caused by shared
    releaseName.
    """
    prod = {f"{k}/{n}" for k, n, _ in _docs(renders["prod"]) if k in CLUSTER_SCOPED}
    dev = {f"{k}/{n}" for k, n, _ in _docs(renders["dev"]) if k in CLUSTER_SCOPED}
    shared = prod & dev
    assert not shared, (
        f"dev and production both render {sorted(shared)}. These kinds have no "
        "namespace, so that is ONE object with two ArgoCD owners, both with "
        "selfHeal. Failure 1 example: shared releaseName collapsed both CPs "
        "onto production's cluster-scoped RBAC."
    )
    # Guard the guard: if RBAC stopped rendering entirely, the disjointness
    # above passes for the wrong reason.
    assert prod, "production rendered no cluster-scoped objects; this test is inert"


def test_dev_s3_bucket_differs_from_production(renders):
    """Dev and production must use separate S3 buckets (Failure 3).

    Warmth GC and base retention operate on S3; a shared bucket means
    production GC and dev artifacts collide. Dev uses embervm-dev;
    production uses embervm.
    """

    def extract_s3_buckets(rendered: str) -> set[str]:
        buckets = set()
        # Look for EMBERVM_STORE_BUCKET env var values
        for match in re.finditer(
            r'name:\s*EMBERVM_STORE_BUCKET\s+value:\s*"([^"]+)"', rendered
        ):
            bucket = match.group(1)
            if bucket:
                buckets.add(bucket)
        return buckets

    prod_buckets = extract_s3_buckets(renders["prod"])
    dev_buckets = extract_s3_buckets(renders["dev"])

    shared = prod_buckets & dev_buckets
    assert not shared, (
        f"dev and production both use S3 bucket(s) {sorted(shared)}. "
        "Warmth GC and base retention operate on S3, so a shared bucket is a "
        "GC collision point: failure 3. Separate buckets are required. Set "
        "noded.store.bucket=embervm-dev in dev/deploy/values.yaml."
    )

    # Guard the guard: if bucket configuration stopped rendering, this passes
    # vacuously.
    assert prod_buckets, (
        "production rendered no S3 bucket configuration; this test is inert. "
        "Check noded.store.bucket in values."
    )
    assert dev_buckets, (
        "dev rendered no S3 bucket configuration; this test is inert. "
        "Check noded.store.bucket in dev/deploy/values.yaml."
    )


def test_dev_does_not_render_production_only_workloads(renders):
    """Dev disables workload CRs for size; assert disables stick (Failure 2).

    Dev exercises task-class lifecycle (sandbox only) for the conformance
    harness, not serving or stateful workloads. Disabling them in values is
    what keeps them out of the render; if chart defaults leaked in, they
    would re-appear. Failure 2 example: noded DaemonSet defaulted on in the
    chart, dev inherited it on all four nodes.
    """

    def find_workload_names(rendered: str) -> set[str]:
        return {name for kind, name, _ in _docs(rendered) if kind == "Workload"}

    prod_workloads = find_workload_names(renders["prod"])
    dev_workloads = find_workload_names(renders["dev"])

    # These should be in production but NOT in dev. Listed in
    # dev/deploy/values.yaml as disabled.
    prod_only = {
        "semgrep",
        "bazel-query",
        "runtime-python",
        "runtime-claude",
        "scratch-postgres",
        "demo-postgres",
        "sandbox-session",
        # ADR embervm/035. Dev has no egress lane (egress.enabled is false there),
        # so a shotter guest could not reach a frontend even if it ran, and its
        # warm-Chromium base is expensive to build for something never served.
        "shotter",
    }

    # If any of these prod-only workloads are still in dev, it means the
    # disable did not stick. This is failure 2: chart defaults leaking in when
    # dev's values go silent on a setting.
    disabled_that_render = prod_only & dev_workloads
    assert not disabled_that_render, (
        f"dev renders Workloads that should be disabled: {sorted(disabled_that_render)}. "
        "These must be disabled in dev/deploy/values.yaml for the conformance "
        "harness to stay small. Failure 2: chart defaults leaked in when "
        "dev's overlay went silent on *Workload.enabled."
    )

    # Guard the guard: if production stopped rendering these, we cannot prove
    # dev's disables worked.
    missing_in_dev = prod_only - dev_workloads
    assert missing_in_dev, (
        "dev disabled Workloads that production does not render; this test is inert. "
        "Check that production still defines the full fleet."
    )


def test_noded_bucket_path_configuration(renders):
    """Dev's noded bucket path must be isolated from production.

    The noded control-plane pod reads EMBERVM_NODED_STORE_BUCKET (the path
    component of the S3 URI for storing noded state, separate from the
    warmth bucket). This must differ across dev and prod to prevent
    cross-environment state sharing. Failure 3.
    """

    def extract_noded_store_paths(rendered: str) -> set[str]:
        paths = set()
        # Look for EMBERVM_NODED_STORE_BUCKET env var values
        for match in re.finditer(
            r'name:\s*EMBERVM_NODED_STORE_BUCKET\s+value:\s*"([^"]+)"', rendered
        ):
            path = match.group(1)
            if path:
                paths.add(path)
        return paths

    prod_paths = extract_noded_store_paths(renders["prod"])
    dev_paths = extract_noded_store_paths(renders["dev"])

    shared = prod_paths & dev_paths
    assert not shared, (
        f"dev and production both use noded store path(s) {sorted(shared)}. "
        "Failure 3: the noded state bucket must be isolated so production and "
        "dev control planes do not collide on state records. Set "
        "noded.store.path in dev values to a -dev suffix."
    )

    # Guard the guard.
    assert prod_paths, (
        "production rendered no noded store path; this test is inert. "
        "Check noded.store.path in values."
    )
    assert dev_paths, (
        "dev rendered no noded store path; this test is inert. "
        "Check noded.store.path in dev values."
    )


def test_brick_renders_default_warmth_heartbeat_env():
    """Brick Deployments claim warmth with safe transition defaults.

    Rendered from the chart defaults, not production: production flipped
    reapUnclaimed on once every brick heartbeated (#4962), and the default
    is what a fresh environment inherits.
    """
    chart = _chart_dir()
    rendered = _render("warmth", [chart / "values.yaml"], ["bricks.enabled=true"])
    brick_deployments = [
        doc
        for kind, _, doc in _docs(rendered)
        if kind == "Deployment" and "app.kubernetes.io/component: noded-brick" in doc
    ]
    assert brick_deployments, (
        "default render produced no brick Deployment; test is inert"
    )

    expected = {
        "EMBERVM_NODED_WARMTH_HEARTBEAT_INTERVAL": "30s",
        "EMBERVM_NODED_WARMTH_STALE_AFTER": "600s",
        "EMBERVM_NODED_REAP_UNCLAIMED_WARMTH": "0",
    }
    for deployment in brick_deployments:
        for name, value in expected.items():
            rendered_env = re.search(
                rf"name:\s*{name}\s+value:\s*\"([^\"]+)\"", deployment
            )
            assert rendered_env, f"brick Deployment is missing {name}"
            assert rendered_env.group(1) == value, (
                f"brick Deployment renders {name}={rendered_env.group(1)!r}, "
                f"want {value!r}"
            )


def test_brick_renders_inert_artifact_encryption_envs():
    """Envelope writing and restore enforcement default off on every brick."""
    chart = _chart_dir()
    rendered = _render("envelope", [chart / "values.yaml"], ["bricks.enabled=true"])
    brick_deployments = [
        doc
        for kind, _, doc in _docs(rendered)
        if kind == "Deployment" and "app.kubernetes.io/component: noded-brick" in doc
    ]
    assert brick_deployments, (
        "default render produced no brick Deployment; this test is inert"
    )

    expected = {
        "EMBERVM_NODED_STORE_ENCRYPT": "false",
        "EMBERVM_NODED_REQUIRE_RESTORE_CAPABILITY": "false",
    }
    for deployment in brick_deployments:
        for name, value in expected.items():
            rendered_env = re.search(
                rf"name:\s*{name}\s+value:\s*\"([^\"]+)\"", deployment
            )
            assert rendered_env, f"brick Deployment is missing {name}"
            assert rendered_env.group(1) == value, (
                f"brick Deployment renders {name}={rendered_env.group(1)!r}, "
                f"want {value!r}"
            )

    control_plane = next(
        doc
        for kind, name, doc in _docs(rendered)
        if kind == "Deployment" and name == "envelope-embervm"
    )
    rendered_env = re.search(
        r'name:\s*EMBERVM_ARTIFACT_ENCRYPTION\s+value:\s*"([^\"]+)"',
        control_plane,
    )
    assert rendered_env, (
        "control-plane Deployment is missing EMBERVM_ARTIFACT_ENCRYPTION"
    )
    assert rendered_env.group(1) == "0"


def test_dev_hostpaths_are_under_production_scratch_never_beside_it(renders):
    """Dev's hostPaths must be UNDER production's, not siblings of it.

    A plain inequality assertion would be wrong and would have passed the bug.
    Dev's scratch was `/var/lib/embervm/scratch-dev`, which differs from
    production's `/var/lib/embervm/scratch` while being a SIBLING of the NVMe
    mount point, so it was a plain directory on the node's root filesystem: the
    only scratch path in this system with no capacity bound, on the node that
    also runs production's brick-16gi-node-4 and brick-2gi. ADR 012's uniform cap
    exists for that, and six base builders writing multi-GB images to an uncapped
    root filesystem is a DiskPressure eviction of production waiting on one
    mkdir.

    So the property is containment, not difference. See #4832, #4837.
    """
    prod_paths = set(_HOSTPATH.findall(renders["prod"]))
    dev_paths = set(_HOSTPATH.findall(renders["dev"]))

    assert dev_paths, "no hostPaths in the dev render; this assertion is inert"

    # Device paths are legitimately shared: /dev/kvm is the same device on the
    # same node for both, and is not storage anything can fill.
    dev_storage = {p for p in dev_paths if not p.startswith("/dev/")}
    prod_storage = {p for p in prod_paths if not p.startswith("/dev/")}

    for path in dev_storage:
        if path in prod_storage:
            raise AssertionError(
                f"dev mounts production's hostPath {path} directly, so the two "
                "environments share the same scratch and production GC can reach "
                "dev artifacts"
            )

        under = any(path.startswith(prod.rstrip("/") + "/") for prod in prod_storage)
        assert under, (
            f"dev hostPath {path} is not under any production hostPath "
            f"{sorted(prod_storage)}. A sibling of the NVMe mount point is a plain "
            "directory on the node's ROOT filesystem with no capacity bound (#4832). "
            "Point it under the mount so it inherits that mount's capacity."
        )


def test_dev_rootfs_paths_are_under_dev_scratch(renders):
    """Every dev rootfsPath must live under dev's own nvmeRoot.

    `rootfsPath` is a SEPARATE key from `noded.firecracker.nvmeRoot`, and
    overriding only the latter is not enough: `_noded-pod.tpl` renders
    BASE_ROOTFS_PATH from rootfsPath and mounts only nvmeRoot, so a rootfsPath
    left pointing at production's scratch is UNBACKED inside the container and
    mkfs writes multi-GB base images to the container's writable layer on the
    node's root filesystem.

    This is the assertion that would have caught it. See #4837.
    """
    dev_paths = [
        p for p in set(_HOSTPATH.findall(renders["dev"])) if not p.startswith("/dev/")
    ]
    assert dev_paths, "no dev hostPaths found; this assertion is inert"

    rootfs_paths = set(_ROOTFS_PATH.findall(renders["dev"]))
    assert rootfs_paths, (
        "no BASE_ROOTFS_PATH values in the dev render. Either the brick renders no "
        "base builders, which is fine, or this regex has drifted and the assertion "
        "is inert. Check before deleting."
    )

    for rootfs in rootfs_paths:
        under = any(rootfs.startswith(mount.rstrip("/") + "/") for mount in dev_paths)
        assert under, (
            f"dev BASE_ROOTFS_PATH {rootfs} is not under any hostPath dev actually "
            f"mounts {sorted(dev_paths)}. The path is unbacked in the container, so "
            "the builder writes a multi-GB image to the node's root filesystem and "
            "rebuilds it on every restart (#4837)."
        )


def test_dev_never_dials_production_control_plane(renders):
    """No dev value may resolve into production's namespace.

    noded streams its full `primed_vm_ids` set to whatever control plane it
    registers with, and adoption is ADDITIVE, so a brick reachable by both
    control planes is cross-control-plane double assignment by design, with no
    attacker required. #4762 puts brick overlap explicitly out of scope for
    exactly this reason.
    """
    dev = renders["dev"]

    for needle, why in (
        (".embervm.svc", "production's service DNS"),
        ("namespace: embervm\n", "production's namespace"),
        ("serviceaccount:embervm:", "production's ServiceAccount"),
    ):
        assert needle not in dev, (
            f"the dev render references {why} ({needle!r}). A dev brick that can "
            "reach production's control plane is double-assigned by design, since "
            "adoption is additive over whatever primed_vm_ids a node reports."
        )


def test_kek_root_renders_item_and_env_only_when_enabled():
    chart = _chart_dir()
    enabled = _render(
        "kek",
        [chart / "values.yaml"],
        ["kekRoot.enabled=true", "kekRoot.onepassword.itemPath=vaults/x/items/y"],
    )
    items = [doc for kind, _, doc in _docs(enabled) if kind == "OnePasswordItem"]
    assert any("name: kek-embervm-kek-root" in doc for doc in items)
    control_plane = next(
        doc
        for kind, name, doc in _docs(enabled)
        if kind == "Deployment" and name == "kek-embervm"
    )
    assert re.search(
        r"name:\s*EMBERVM_KEK_ROOT\s+valueFrom:\s+secretKeyRef:\s+name:\s*kek-embervm-kek-root\s+key:\s*root",
        control_plane,
        re.S,
    )
    assert re.search(
        r'name:\s*EMBERVM_KEK_ROOT_GENERATION\s+value:\s*"1"',
        control_plane,
        re.S,
    )
    disabled = _render("kek", [chart / "values.yaml"])
    assert "EMBERVM_KEK_ROOT" not in disabled


def test_kek_root_rotation_renders_one_explicit_previous_generation():
    chart = _chart_dir()
    rendered = _render(
        "kek-rotate",
        [chart / "values.yaml"],
        [
            "kekRoot.enabled=true",
            "kekRoot.generation=2",
            "kekRoot.onepassword.itemPath=vaults/x/items/current",
            "kekRoot.previous.enabled=true",
            "kekRoot.previous.generation=1",
            "kekRoot.previous.onepassword.itemPath=vaults/x/items/previous",
        ],
    )
    items = [doc for kind, _, doc in _docs(rendered) if kind == "OnePasswordItem"]
    assert any("name: kek-rotate-embervm-kek-root" in doc for doc in items)
    assert any("name: kek-rotate-embervm-kek-root-previous" in doc for doc in items)

    control_plane = next(
        doc
        for kind, name, doc in _docs(rendered)
        if kind == "Deployment" and name == "kek-rotate-embervm"
    )
    assert re.search(
        r'name:\s*EMBERVM_KEK_ROOT_GENERATION\s+value:\s*"2"',
        control_plane,
        re.S,
    )
    assert re.search(
        r"name:\s*EMBERVM_KEK_ROOT_PREVIOUS\s+valueFrom:\s+secretKeyRef:\s+name:\s*kek-rotate-embervm-kek-root-previous\s+key:\s*root",
        control_plane,
        re.S,
    )
    assert re.search(
        r'name:\s*EMBERVM_KEK_ROOT_PREVIOUS_GENERATION\s+value:\s*"1"',
        control_plane,
        re.S,
    )


def test_customer_kms_config_renders_only_from_an_operator_named_secret():
    chart = _chart_dir()
    enabled = _render(
        "customer-kms",
        [chart / "values.yaml"],
        ["customerKms.secretName=alice-kms-grant", "customerKms.secretKey=oracle.json"],
    )
    control_plane = next(
        doc
        for kind, name, doc in _docs(enabled)
        if kind == "Deployment" and name == "customer-kms-embervm"
    )
    assert re.search(
        r'name:\s*EMBERVM_CUSTOMER_KMS_CONFIG\s+valueFrom:\s+secretKeyRef:\s+name:\s*"?alice-kms-grant"?\s+key:\s*"?oracle\.json"?',
        control_plane,
        re.S,
    )

    disabled = _render("customer-kms", [chart / "values.yaml"])
    assert "EMBERVM_CUSTOMER_KMS_CONFIG" not in disabled


def test_session_banked_ttl_at_or_above_gc_session_ttl_fails_render() -> None:
    """#4336: reject banked TTLs that race the S3 warmth GC boundary."""
    ok = _render_with_set(
        "e",
        [
            "piRuntimeWorkload.enabled=true",
            "piRuntimeWorkload.session.bankedTtlSeconds=604799",
        ],
    )
    assert "bankedTtlSeconds: 604799" in ok

    with pytest.raises(
        RuntimeError, match="meets or exceeds the S3 warmth GC session TTL"
    ):
        _render_with_set(
            "e",
            [
                "piRuntimeWorkload.enabled=true",
                "piRuntimeWorkload.session.bankedTtlSeconds=604800",
            ],
        )

    # The bound follows the configured floor, not only the default.
    with pytest.raises(
        RuntimeError, match="claudeRuntimeWorkload.session.bankedTtlSeconds"
    ):
        _render_with_set(
            "e",
            ["claudeRuntimeWorkload.enabled=true", "warmthS3Gc.sessionTtlMs=1800000"],
        )


_WORKLOAD_RULES = [
    {
        "apiGroups": ["embervm.dev"],
        "resources": ["workloads"],
        "verbs": ["get", "list", "watch"],
    },
    {
        "apiGroups": ["embervm.dev"],
        "resources": ["workloads/status"],
        "verbs": ["get", "update", "patch"],
    },
]
_TOKEN_REVIEW_RULE = {
    "apiGroups": ["authentication.k8s.io"],
    "resources": ["tokenreviews"],
    "verbs": ["create"],
}
_POD_RULE = {"apiGroups": [""], "resources": ["pods"], "verbs": ["list", "patch"]}
_DEFAULT_CLUSTER_RULES = [
    *_WORKLOAD_RULES,
    _TOKEN_REVIEW_RULE,
    {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"]},
    {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get"]},
    {
        "apiGroups": ["apps"],
        "resources": ["deployments/scale"],
        "verbs": ["get", "patch"],
    },
]
_RBAC_KINDS = {"Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding"}


def _rbac_objects(documents):
    return {
        (doc["kind"], doc["metadata"].get("namespace"), doc["metadata"]["name"]): {
            key: doc[key] for key in ("rules", "roleRef", "subjects") if key in doc
        }
        for doc in documents
        if isinstance(doc, dict) and doc.get("kind") in _RBAC_KINDS
    }


def _role_pair(kind, name, namespace, rules, service_account, service_namespace):
    return {
        (kind, namespace, name): {"rules": rules},
        (f"{kind}Binding", namespace, name): {
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": kind,
                "name": name,
            },
            "subjects": [
                {
                    "kind": "ServiceAccount",
                    "name": service_account,
                    "namespace": service_namespace,
                }
            ],
        },
    }


def _effective_grants(objects, service_account, namespace):
    """Resolve every additive binding, including a RoleBinding to a ClusterRole."""
    subject = {
        "kind": "ServiceAccount",
        "name": service_account,
        "namespace": namespace,
    }
    grants = []
    for (kind, binding_namespace, _), binding in objects.items():
        if kind not in {"RoleBinding", "ClusterRoleBinding"}:
            continue
        if subject not in binding["subjects"]:
            continue
        ref = binding["roleRef"]
        role_namespace = None if ref["kind"] == "ClusterRole" else binding_namespace
        role = objects[ref["kind"], role_namespace, ref["name"]]
        grants.extend((binding_namespace, rule) for rule in role["rules"])
    return sorted(grants, key=repr)


def _broker_rules(names):
    return [
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "resourceNames": [f"embervm-oauth-grant-{name}" for name in names],
            "verbs": ["get", "update"],
        }
    ]


@pytest.mark.parametrize(
    "values_names, bricks_enabled, grant_names",
    [
        ([], False, ["codex-cluster"]),
        (["PROD_VALUES"], True, ["codex-cluster"]),
        (["PROD_VALUES", "GKE_VALUES"], True, ["codex-cluster", "agent-mcp"]),
    ],
    ids=["defaults", "home", "gke"],
)
def test_default_rbac_preserves_all_rule_and_binding_contracts(
    values_names, bricks_enabled, grant_names
):
    release = "embervm"
    name = "embervm-embervm"
    documents = list(
        yaml.safe_load_all(
            _render(release, [Path(os.environ[key]) for key in values_names])
        )
    )
    expected = _role_pair(
        "ClusterRole", name, None, _DEFAULT_CLUSTER_RULES, name, release
    )
    if bricks_enabled:
        expected.update(
            _role_pair(
                "Role", f"{name}-brick-pods", release, [_POD_RULE], name, release
            )
        )
    expected.update(
        _role_pair(
            "Role",
            f"{name}-tokenbroker",
            release,
            _broker_rules(grant_names),
            f"{name}-tokenbroker",
            release,
        )
    )
    assert _rbac_objects(documents) == expected


def _scoped_documents(
    tmp_path, *, mode="observe", secrets=(), bricks=True, classes=True
):
    # Custom class and chart names exercise the exact Deployment identity seam;
    # a static floor is deliberately present and must never receive scale RBAC.
    class_template = yaml.safe_load((_chart_dir() / "values.yaml").read_text())[
        "bricks"
    ]["classes"][0]
    class_values = (
        [dict(class_template, name=name) for name in ("small", "large")]
        if classes
        else []
    )
    override = tmp_path / "scoped-rbac.yaml"
    override.write_text(
        yaml.safe_dump(
            {
                "nameOverride": "lane",
                "rbac": {"scope": "namespace", "secretNames": list(secrets)},
                "bricks": {
                    "enabled": bricks,
                    "autoscale": {"mode": mode},
                    "classes": class_values,
                    "nodeFloors": [{"node": "fixture-node", "class": "small"}]
                    if classes
                    else [],
                },
                "conformance": {"enabled": True},
                "tokenBroker": {
                    "grants": [
                        {
                            "name": "recovery-only",
                            "provider": "codex-chatgpt",
                            "fqdn": "auth.openai.com",
                        }
                    ]
                },
            }
        )
    )
    return [
        doc
        for doc in yaml.safe_load_all(_render("recovery", [override]))
        if isinstance(doc, dict)
    ]


@pytest.mark.parametrize("mode", ["off", "observe", "up", "full"])
@pytest.mark.parametrize("secret_names", [[], ["fixture-a", "fixture.b"]])
def test_namespace_rbac_effective_grants_and_subject_boundaries(
    tmp_path, mode, secret_names
):
    documents = _scoped_documents(tmp_path, mode=mode, secrets=secret_names)
    objects = _rbac_objects(documents)
    name = "recovery-lane"
    runtime_rules = list(_WORKLOAD_RULES)
    if secret_names:
        runtime_rules.append(
            {
                "apiGroups": [""],
                "resources": ["secrets"],
                "resourceNames": secret_names,
                "verbs": ["get"],
            }
        )
    deployments = [f"{name}-noded-brick-small", f"{name}-noded-brick-large"]
    runtime_rules.extend(
        [
            {
                "apiGroups": ["apps"],
                "resources": ["deployments"],
                "resourceNames": deployments,
                "verbs": ["get"],
            },
            {
                "apiGroups": ["apps"],
                "resources": ["deployments/scale"],
                "resourceNames": deployments,
                "verbs": ["get", "patch"],
            },
        ]
    )
    expected = _role_pair(
        "ClusterRole", name, None, [_TOKEN_REVIEW_RULE], name, "recovery"
    )
    expected.update(
        _role_pair(
            "Role", f"{name}-runtime", "recovery", runtime_rules, name, "recovery"
        )
    )
    if mode == "full":
        expected.update(
            _role_pair(
                "Role", f"{name}-brick-pods", "recovery", [_POD_RULE], name, "recovery"
            )
        )
    expected.update(
        _role_pair(
            "Role",
            f"{name}-tokenbroker",
            "recovery",
            _broker_rules(["recovery-only"]),
            f"{name}-tokenbroker",
            "recovery",
        )
    )
    assert objects == expected
    cp_grants = [(None, _TOKEN_REVIEW_RULE)] + [
        ("recovery", rule) for rule in runtime_rules
    ]
    if mode == "full":
        cp_grants.append(("recovery", _POD_RULE))
    assert _effective_grants(objects, name, "recovery") == sorted(cp_grants, key=repr)
    assert _effective_grants(objects, f"{name}-tokenbroker", "recovery") == [
        ("recovery", _broker_rules(["recovery-only"])[0])
    ]
    service_accounts = {
        doc["metadata"]["name"]
        for doc in documents
        if doc.get("kind") == "ServiceAccount"
    }
    for unprivileged in (f"{name}-noded", f"{name}-conformance"):
        assert unprivileged in service_accounts
        assert _effective_grants(objects, unprivileged, "recovery") == []
    # A SA with the same name in a different namespace receives no authority.
    assert _effective_grants(objects, name, "embervm") == []
    rendered_deployments = {
        doc["metadata"]["name"] for doc in documents if doc.get("kind") == "Deployment"
    }
    assert set(deployments) <= rendered_deployments
    assert f"{name}-noded-brick-small-fixture-node" in rendered_deployments
    control = next(
        doc
        for doc in documents
        if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == name
    )
    env = {
        entry["name"]: entry.get("value")
        for container in control["spec"]["template"]["spec"]["containers"]
        for entry in container.get("env", [])
    }
    assert env["EMBERVM_BRICK_DEPLOYMENT_PREFIX"] == f"{name}-noded-brick-"


@pytest.mark.parametrize("bricks, classes", [(False, True), (True, False)])
def test_namespace_rbac_omits_scale_rule_when_no_class_deployments(
    tmp_path, bricks, classes
):
    documents = _scoped_documents(tmp_path, bricks=bricks, classes=classes)
    objects = _rbac_objects(documents)
    grants = _effective_grants(objects, "recovery-lane", "recovery")
    assert grants == sorted(
        [(None, _TOKEN_REVIEW_RULE), *[("recovery", rule) for rule in _WORKLOAD_RULES]],
        key=repr,
    )
    assert not any(
        doc.get("kind") == "Deployment" and "-brick-" in doc["metadata"]["name"]
        for doc in documents
    )


@pytest.mark.parametrize(
    "rbac, message",
    [
        ({"scope": "namespaced"}, "rbac.scope must be cluster or namespace"),
        ({"scope": False}, "rbac.scope must be cluster or namespace"),
        (
            {"scope": "namespace", "secretNames": "fixture"},
            "rbac.secretNames must be a list",
        ),
        *[
            (
                {"scope": "namespace", "secretNames": [name]},
                "rbac.secretNames entries must be valid Secret names",
            )
            for name in ("", " ", "*", "UPPER", "fixture/other", "a" * 254, 1)
        ],
    ],
)
def test_namespace_rbac_rejects_invalid_inputs(tmp_path, rbac, message):
    override = tmp_path / "invalid-rbac.yaml"
    override.write_text(yaml.safe_dump({"rbac": rbac}))
    with pytest.raises(RuntimeError, match=re.escape(message)):
        _render("recovery", [override])


# The INACTIVE GKE recovery preset (values-recovery-gke.yaml) renders over
# CHART DEFAULTS only, never production or historical dev values. These checks
# load the actual values file through RECOVERY_VALUES, not a hand copy.
_RECOVERY_CP = "embervm-dev-embervm"
_RECOVERY_NS = "embervm-dev"
_RECOVERY_PROGRESS_URL = "http://monolith-dev.monolith-dev:8091/ingest/progress"
_RECOVERY_AGENT_MCP_URL = "http://monolith-dev.monolith-dev:8000/mcp"
_RECOVERY_ENV = {
    "EMBER_PROGRESS_URL": _RECOVERY_PROGRESS_URL,
    "EMBER_AGENT_MCP_URL": _RECOVERY_AGENT_MCP_URL,
}
_RECOVERY_WORKLOADS = {"claude-runtime", "sandbox-dev-python"}
_RECOVERY_CLASSES = ["1gi", "2gi", "4gi", "8gi", "16gi"]


def _recovery_values() -> Path:
    return Path(os.environ["RECOVERY_VALUES"])


@pytest.fixture(scope="module")
def recovery_render() -> str:
    return _render(_RECOVERY_NS, [_chart_dir() / "values.yaml", _recovery_values()])


def test_recovery_guest_kernel_env_values_file() -> None:
    default_args = _kernel_boot_args(_chart_dir() / "values.yaml")
    boot_args, env = _decode_kernel_env(
        yaml.safe_load(_recovery_values().read_text())["noded"]["firecracker"][
            "kernelBootArgs"
        ]
    )
    assert boot_args == default_args
    assert env == _RECOVERY_ENV


def test_recovery_guest_kernel_env_rendered_on_every_noded(recovery_render) -> None:
    rendered = recovery_render
    noded_args = []
    for document in yaml.safe_load_all(rendered):
        if not isinstance(document, dict):
            continue
        pod_spec = document.get("spec", {}).get("template", {}).get("spec", {})
        for container in pod_spec.get("containers", []):
            if container.get("name") == "noded":
                env = {entry["name"]: entry for entry in container.get("env", [])}
                noded_args.append(env["EMBERVM_NODED_KERNEL_BOOT_ARGS"]["value"])
    assert noded_args, "expected at least one rendered noded container"
    default_args = _kernel_boot_args(_chart_dir() / "values.yaml")
    for args in noded_args:
        boot_args, env = _decode_kernel_env(args)
        assert boot_args == default_args
        assert env == _RECOVERY_ENV


def test_recovery_namespace_rbac_matches_contract(recovery_render) -> None:
    documents = [
        doc for doc in yaml.safe_load_all(recovery_render) if isinstance(doc, dict)
    ]
    objects = _rbac_objects(documents)
    assert objects, "recovery render produced no RBAC objects; this test is inert"
    assert {kind for kind, _, _ in objects} <= {
        "ClusterRole",
        "ClusterRoleBinding",
        "Role",
        "RoleBinding",
    }
    cluster = {key: value for key, value in objects.items() if key[1] is None}
    assert set(cluster) == {
        ("ClusterRole", None, _RECOVERY_CP),
        ("ClusterRoleBinding", None, _RECOVERY_CP),
    }
    deployments = [f"{_RECOVERY_CP}-noded-brick-{c}" for c in _RECOVERY_CLASSES]
    runtime_rules = list(_WORKLOAD_RULES)
    runtime_rules.extend(
        [
            {
                "apiGroups": ["apps"],
                "resources": ["deployments"],
                "resourceNames": deployments,
                "verbs": ["get"],
            },
            {
                "apiGroups": ["apps"],
                "resources": ["deployments/scale"],
                "resourceNames": deployments,
                "verbs": ["get", "patch"],
            },
        ]
    )
    expected = _role_pair(
        "ClusterRole",
        _RECOVERY_CP,
        None,
        [_TOKEN_REVIEW_RULE],
        _RECOVERY_CP,
        _RECOVERY_NS,
    )
    expected.update(
        _role_pair(
            "Role",
            f"{_RECOVERY_CP}-runtime",
            _RECOVERY_NS,
            runtime_rules,
            _RECOVERY_CP,
            _RECOVERY_NS,
        )
    )
    expected.update(
        _role_pair(
            "Role",
            f"{_RECOVERY_CP}-tokenbroker",
            _RECOVERY_NS,
            _broker_rules(["codex-cluster", "recovery-agent-mcp"]),
            f"{_RECOVERY_CP}-tokenbroker",
            _RECOVERY_NS,
        )
    )
    assert objects == expected
    assert _effective_grants(objects, _RECOVERY_CP, _RECOVERY_NS) == sorted(
        [(None, _TOKEN_REVIEW_RULE)] + [(_RECOVERY_NS, rule) for rule in runtime_rules],
        key=repr,
    )
    service_accounts = {
        doc["metadata"]["name"]
        for doc in documents
        if doc.get("kind") == "ServiceAccount"
    }
    assert f"{_RECOVERY_CP}-noded" in service_accounts
    assert _effective_grants(objects, f"{_RECOVERY_CP}-noded", _RECOVERY_NS) == []


def test_recovery_lane_isolation(recovery_render) -> None:
    rendered = recovery_render
    documents = [d for d in yaml.safe_load_all(rendered) if isinstance(d, dict)]
    assert len(documents) > 5, "recovery render looks inert"
    # Template comments describe production and floor selectors too. Check
    # resource fields, not those explanatory comments in Helm's output.
    for document in documents:
        assert document["metadata"].get("namespace") in {None, _RECOVERY_NS}
        assert document["kind"] not in {"DaemonSet", "CiliumNetworkPolicy"}
    bricks = {
        doc["metadata"]["name"]: doc
        for doc in yaml.safe_load_all(rendered)
        if isinstance(doc, dict)
        and doc.get("kind") == "Deployment"
        and any(
            c["name"] == "noded" for c in doc["spec"]["template"]["spec"]["containers"]
        )
    }
    assert set(bricks) == {
        f"{_RECOVERY_CP}-noded-brick-{size}" for size in _RECOVERY_CLASSES
    }, "unexpected or missing brick/floor Deployment"
    live = [
        name
        for name, doc in bricks.items()
        if doc.get("spec", {}).get("replicas", 0) == 1
    ]
    assert live == [f"{_RECOVERY_CP}-noded-brick-8gi"], (
        f"expected exactly one live brick (8gi), got {sorted(live)}"
    )
    names = {name for kind, name, _ in _docs(rendered) if kind == "Workload"}
    assert names == _RECOVERY_WORKLOADS, (
        f"recovery renders Workloads {sorted(names)}, want "
        f"{sorted(_RECOVERY_WORKLOADS)}"
    )
    assert "build-sandbox-python-rootfs" in rendered
    assert "build-runtime-claude-rootfs" in rendered
    for retired in (
        "build-sandbox-go-rootfs",
        "build-sandbox-rust-rootfs",
        "build-semgrep-rootfs",
        "build-runtime-pi-rootfs",
        "build-shotter-rootfs",
        "build-bazel-query-rootfs",
    ):
        assert retired not in rendered, (
            f"recovery still bakes retired builder {retired}"
        )
    for path in _HOSTPATH.findall(rendered):
        if path.startswith("/dev/"):
            continue
        assert path == "/var/lib/embervm/scratch/recovery" or path.startswith(
            "/var/lib/embervm/scratch/recovery/"
        ), f"recovery hostPath {path} escapes the disjoint recovery subtree"
    for rootfs in _ROOTFS_PATH.findall(rendered):
        assert rootfs.startswith("/var/lib/embervm/scratch/recovery/"), (
            f"recovery BASE_ROOTFS_PATH {rootfs} escapes the recovery subtree"
        )
    documents = [d for d in yaml.safe_load_all(rendered) if isinstance(d, dict)]
    items = [
        d["spec"]["itemPath"] for d in documents if d.get("kind") == "OnePasswordItem"
    ]
    assert set(items) == {
        "vaults/k8s-homelab/items/embervm-recovery-ghcr-read",
        "vaults/k8s-homelab/items/embervm-recovery-oplog-db",
        "vaults/k8s-homelab/items/embervm-recovery-noded-token",
        "vaults/k8s-homelab/items/embervm-recovery-store",
        "vaults/k8s-homelab/items/embervm-recovery-kek-root",
    }
    for document in bricks.values():
        assert document["spec"]["replicas"] == (
            1 if document["metadata"]["name"].endswith("-8gi") else 0
        )
        pod = document["spec"]["template"]["spec"]
        assert pod["nodeSelector"] == {"homelab.io/firecracker": "true"}
        assert pod["imagePullSecrets"] == [
            {"name": "embervm-recovery-imagepull-secret"}
        ]
        assert {c["name"] for c in pod["initContainers"]} == {
            "build-runtime-claude-rootfs",
            "build-sandbox-python-rootfs",
        }
    workloads = {
        d["metadata"]["name"]: d["spec"]
        for d in documents
        if d.get("kind") == "Workload"
    }
    session = workloads["claude-runtime"]
    assert session["concurrency"] == {"floor": 0, "cap": 1}
    assert session["session"]["maxSessions"] == 1
    assert session["persistence"]["filesystem"]["enabled"] is True
    defaults = yaml.safe_load((_chart_dir() / "values.yaml").read_text())
    for workload in workloads.values():
        assert workload["source"]["image"]["initEnv"]["EMBER_HYPERVISOR_EPOCH"] == (
            defaults["hypervisorEpoch"] + "-recovery-gke-1"
        )


def test_recovery_rendered_credentials_and_capacity(recovery_render) -> None:
    documents = [d for d in yaml.safe_load_all(recovery_render) if isinstance(d, dict)]
    deployments = {
        d["metadata"]["name"]: d for d in documents if d.get("kind") == "Deployment"
    }

    def container_env(deployment, container_name):
        containers = deployment["spec"]["template"]["spec"]["containers"]
        container = next(c for c in containers if c["name"] == container_name)
        return {e["name"]: e for e in container["env"]}

    control_env = container_env(deployments[_RECOVERY_CP], "control-plane")
    assert (
        not {
            "EMBERVM_BASE_RETENTION_SWEEP",
            "EMBERVM_BASE_RETENTION_DISK_DRIVEN",
            "EMBERVM_BASE_REMOTE_RETENTION_SWEEP",
            "EMBERVM_WARMTH_RETENTION_SWEEP",
            "EMBERVM_WARMTH_S3_GC",
            "EMBERVM_GRPC_CONNECTION_SWEEP_ENABLED",
        }
        & control_env.keys()
    )
    for name, deployment in deployments.items():
        if "-noded-brick-" not in name:
            continue
        env = container_env(deployment, "noded")
        assert env["EMBERVM_NODED_MAX_LIVE_VMS"]["value"] == "1"
        assert env["EMBERVM_NODED_STORE_BUCKET"]["value"] == "h0melab-ember-recovery"
        assert env["EMBERVM_NODED_REQUIRE_RESTORE_CAPABILITY"]["value"] == "true"
        if name.endswith("-8gi"):
            assert env["EMBERVM_NODED_WARM_RESTORE_WITH_VOLUME"]["value"] == "true"
        else:
            assert "EMBERVM_NODED_WARM_RESTORE_WITH_VOLUME" not in env
        sidecar = container_env(deployment, "egress-proxy")
        catalog = json.loads(sidecar["EGRESS_SECRETS"]["value"])
        assert {e["brokerGrant"] for e in catalog} == {
            "codex-cluster",
            "recovery-agent-mcp",
        }
        mcp = next(e for e in catalog if e["brokerGrant"] == "recovery-agent-mcp")
        assert mcp["egressTo"] == ["monolith-dev.monolith-dev"]
        assert mcp["injectAlwaysPaths"] == ["/mcp", "/mcp/"]
        assert mcp["plaintextUpstream"] is True
    assert any(d.get("kind") == "Certificate" for d in documents)


def test_recovery_disarms_destructive_defaults() -> None:
    values = yaml.safe_load(_recovery_values().read_text())
    assert values["noded"]["enabled"] is False
    assert values["scratchPrep"]["enabled"] is False
    assert values["bricks"]["autoscale"]["mode"] == "observe"
    assert values["bricks"]["nodeFloors"] == []
    assert values["conformance"]["enabled"] is False
    assert values["servingEnvoy"]["enabled"] is False
    assert values["noded"]["networkPolicy"]["enabled"] is False
    assert values["tokenBroker"]["networkPolicy"]["enabled"] is False
    assert values["baseRetention"]["sweepEnabled"] == ""
    assert values["baseRetention"]["remoteSweepEnabled"] == ""
    assert values["warmthRetention"]["sweepEnabled"] == ""
    assert values["warmthS3Gc"]["enabled"] == ""
    assert values["rootfsReclaim"]["enabled"] == ""
    assert values["statefulSweeper"]["pressureBanking"]["enabled"] is False
    assert values["noded"]["store"]["encrypt"] is True
    assert values["noded"]["requireRestoreCapability"] is True
    assert values["artifactEncryption"]["enabled"] is True
    assert values["tokenBroker"]["authentik"]["username"] == "kg-agent-recovery-sa"
    assert values["noded"]["store"]["bucket"] == "h0melab-ember-recovery"
    assert values["opLog"]["postgres"]["secretName"] == (
        "monolith-dev-pg-embervm-oplog"
    )
