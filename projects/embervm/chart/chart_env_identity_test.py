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

import os
import re
import subprocess
from pathlib import Path

import pytest

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


def test_brick_renders_default_warmth_heartbeat_env(renders):
    """Brick Deployments claim warmth with safe transition defaults."""
    brick_deployments = [
        doc
        for kind, _, doc in _docs(renders["prod"])
        if kind == "Deployment" and "app.kubernetes.io/component: noded-brick" in doc
    ]
    assert brick_deployments, "production rendered no brick Deployment; test is inert"

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
