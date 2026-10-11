"""Least-privilege posture pins: the brick's ServiceAccount token reaches noded only,
and the production control plane's RBAC is confined to its namespace."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

_CHART_DIR = Path(__file__).resolve().parent
_PROD_VALUES = Path(
    os.environ.get("PROD_VALUES", _CHART_DIR.parent / "deploy" / "values.yaml")
)
_GKE_VALUES = Path(
    os.environ.get("GKE_VALUES", _CHART_DIR.parent / "deploy" / "values-gke.yaml")
)

_SA_MOUNT = "/var/run/secrets/kubernetes.io/serviceaccount"


def _render(
    release: str, values: list[Path], overrides: list[str] | None = None
) -> list[dict[str, Any]]:
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        release,
        str(_CHART_DIR),
        "--namespace",
        release,
    ]
    for path in values:
        argv += ["--values", str(path)]
    for override in overrides or []:
        argv += ["--set", override]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def _brick_pods(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pods = []
    for document in documents:
        if document.get("kind") not in {"DaemonSet", "Deployment"}:
            continue
        spec = document["spec"]["template"]["spec"]
        if any(container["name"] == "noded" for container in spec["containers"]):
            pods.append(spec)
    assert pods, "expected at least one rendered brick pod"
    return pods


def test_brick_pod_projects_the_sa_token_into_noded_only() -> None:
    for pod in _brick_pods(_render("embervm", [_PROD_VALUES, _GKE_VALUES])):
        assert pod["automountServiceAccountToken"] is False

        volumes = {volume["name"]: volume for volume in pod["volumes"]}
        projected = volumes["noded-sa-token"]["projected"]
        sources = [next(iter(source)) for source in projected["sources"]]
        assert sources == ["serviceAccountToken", "configMap", "downwardAPI"]
        assert projected["sources"][0]["serviceAccountToken"]["path"] == "token"

        for container in pod["containers"] + pod.get("initContainers", []):
            mounts = [
                mount
                for mount in container.get("volumeMounts", [])
                if mount["mountPath"] == _SA_MOUNT
            ]
            if container["name"] == "noded":
                assert (
                    mounts
                    and mounts[0]["name"] == "noded-sa-token"
                    and mounts[0]["readOnly"] is True
                )
            else:
                assert not mounts, (
                    f"{container['name']} mounts the ServiceAccount token"
                )
            assert (
                not any(
                    mount["name"] == "noded-sa-token"
                    for mount in container.get("volumeMounts", [])
                )
                or container["name"] == "noded"
            )


def test_production_control_plane_rbac_is_namespace_scoped() -> None:
    documents = _render("embervm", [_PROD_VALUES, _GKE_VALUES])
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for document in documents:
        by_kind.setdefault(document["kind"], []).append(document)

    cluster_roles = [
        role
        for role in by_kind["ClusterRole"]
        if role["metadata"]["name"] == "embervm-embervm"
    ]
    assert len(cluster_roles) == 1
    cluster_rules = cluster_roles[0]["rules"]
    cluster_resources = {
        resource for rule in cluster_rules for resource in rule["resources"]
    }
    # Only what cannot be namespaced stays on the ClusterRole.
    assert cluster_resources == {"nodes", "tokenreviews"}
    for rule in cluster_rules:
        assert "secrets" not in rule["resources"]
        assert "deployments/scale" not in rule["resources"]

    runtime_roles = [
        role
        for role in by_kind["Role"]
        if role["metadata"]["name"] == "embervm-embervm-runtime"
    ]
    assert len(runtime_roles) == 1
    runtime_resources = {
        resource for rule in runtime_roles[0]["rules"] for resource in rule["resources"]
    }
    assert {
        "workloads",
        "workloads/status",
        "deployments",
        "deployments/scale",
    } <= runtime_resources
    # No production workload declares a control-plane-read secretRef.
    assert "secrets" not in runtime_resources


def _assert_archive_pod_access(mode: str, gate_enabled: bool) -> None:
    documents = _render(
        "embervm",
        [_PROD_VALUES, _GKE_VALUES],
        [
            "rbac.scope=namespace",
            "bricks.enabled=true",
            f"bricks.autoscale.mode={mode}",
            f"bricks.autoscale.archiveAckGate.enabled={str(gate_enabled).lower()}",
        ],
    )
    pod_rules = [
        (role, rule)
        for role in documents
        if role["kind"] in {"Role", "ClusterRole"}
        for rule in role.get("rules", [])
        if "pods" in rule["resources"]
    ]
    bindings = [
        document
        for document in documents
        if document["kind"] == "RoleBinding"
        and document["roleRef"]["name"] == "embervm-embervm-brick-pods"
    ]
    if mode != "full" and not gate_enabled:
        assert pod_rules == []
        assert bindings == []
        return

    assert len(pod_rules) == 1
    role, rule = pod_rules[0]
    assert role["kind"] == "Role"
    assert role["metadata"]["name"] == "embervm-embervm-brick-pods"
    assert role["metadata"]["namespace"] == "embervm"
    assert rule["apiGroups"] == [""]
    assert rule["resources"] == ["pods"]
    assert rule["verbs"] == (["list", "patch"] if mode == "full" else ["list"])
    assert len(bindings) == 1
    assert bindings[0]["subjects"][0]["name"] == "embervm-embervm"


def test_archive_observation_pod_access_is_read_only() -> None:
    for mode in ["observe", "up", "full"]:
        for gate_enabled in [False, True]:
            _assert_archive_pod_access(mode, gate_enabled)
