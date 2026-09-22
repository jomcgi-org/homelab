"""Rendered behavior checks for noded's optional SPIFFE mTLS listener."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parent
_GKE_VALUES = _CHART_DIR.parent / "deploy" / "values-gke.yaml"


def _render(
    release: str,
    settings: list[str] | None = None,
    values: list[Path] | None = None,
) -> list[dict[str, Any]]:
    helm_bin = os.environ.get("HELM_BIN", "helm")
    argv = [
        helm_bin,
        "template",
        release,
        str(_CHART_DIR),
        "--namespace",
        release,
    ]
    for path in values or []:
        argv += ["--values", str(path)]
    for setting in settings or []:
        argv += ["--set", setting]
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def _noded_pods(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pods = []
    for document in documents:
        if document.get("kind") not in {"DaemonSet", "Deployment"}:
            continue
        spec = document["spec"]["template"]["spec"]
        if any(container["name"] == "noded" for container in spec["containers"]):
            pods.append(spec)
    assert pods, "expected at least one rendered noded pod"
    return pods


def _noded_container(pod: dict[str, Any]) -> dict[str, Any]:
    return next(
        container for container in pod["containers"] if container["name"] == "noded"
    )


def _named(items: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in items or []}


def _service(documents: list[dict[str, Any]]) -> dict[str, Any]:
    matches = [
        document
        for document in documents
        if document.get("kind") == "Service"
        and document["metadata"]["name"].endswith("-noded")
    ]
    assert len(matches) == 1
    return matches[0]


def _noded_policy(documents: list[dict[str, Any]]) -> dict[str, Any]:
    matches = [
        document
        for document in documents
        if document.get("kind") == "CiliumNetworkPolicy"
        and document["metadata"]["name"].endswith("-noded")
    ]
    assert len(matches) == 1
    return matches[0]


def _control_plane_service_account(
    documents: list[dict[str, Any]], release: str
) -> str:
    control_plane = next(
        document
        for document in documents
        if document.get("kind") == "Deployment"
        and document["metadata"]["name"] == f"{release}-embervm"
    )
    return control_plane["spec"]["template"]["spec"]["serviceAccountName"]


@pytest.mark.parametrize("values", [[], [_GKE_VALUES]])
def test_default_and_gke_overlay_leave_noded_spiffe_fully_off(
    values: list[Path],
) -> None:
    documents = _render("noded-off", values=values)
    for pod in _noded_pods(documents):
        container = _noded_container(pod)
        env = _named(container.get("env"))
        ports = _named(container.get("ports"))
        mounts = _named(container.get("volumeMounts"))
        volumes = _named(pod.get("volumes"))
        assert "EMBERVM_NODED_SPIFFE_ENABLED" not in env
        assert "EMBERVM_NODED_TLS_LISTEN_ADDR" not in env
        assert "EMBERVM_NODED_SPIFFE_CLIENT_IDS" not in env
        assert "SPIFFE_ENDPOINT_SOCKET" not in env
        assert "grpc-tls" not in ports
        assert "spiffe-workload-api" not in mounts
        assert "spiffe-workload-api" not in volumes
        assert ports["grpc"]["containerPort"] == 9090

    assert set(_named(_service(documents)["spec"]["ports"])) == {"grpc"}


def test_enabled_listener_wires_every_pod_service_and_policy_consistently() -> None:
    release = "noded-tls"
    documents = _render(
        release,
        [
            "noded.spiffe.enabled=true",
            "noded.spiffe.grpcTlsPort=19443",
            "noded.networkPolicy.enabled=true",
        ],
    )
    pods = _noded_pods(documents)
    control_plane_service_account = _control_plane_service_account(documents, release)
    for pod in pods:
        container = _noded_container(pod)
        env = _named(container["env"])
        ports = _named(container["ports"])
        mounts = _named(container["volumeMounts"])
        volumes = _named(pod["volumes"])
        expected_client = (
            "spiffe://embervm.jomcgi.dev/"
            f"ns/{release}/sa/{control_plane_service_account}"
        )
        assert env["EMBERVM_NODED_SPIFFE_ENABLED"]["value"] == "true"
        assert env["EMBERVM_NODED_TLS_LISTEN_ADDR"]["value"] == ":19443"
        assert env["EMBERVM_NODED_SPIFFE_CLIENT_IDS"]["value"] == expected_client
        assert env["SPIFFE_ENDPOINT_SOCKET"]["value"] == (
            "unix:///spiffe-workload-api/spire-agent.sock"
        )
        assert ports["grpc-tls"]["containerPort"] == 19443
        assert mounts["spiffe-workload-api"] == {
            "name": "spiffe-workload-api",
            "mountPath": "/spiffe-workload-api",
            "readOnly": True,
        }
        assert volumes["spiffe-workload-api"]["csi"] == {
            "driver": "csi.spiffe.io",
            "readOnly": True,
        }

    service_ports = _named(_service(documents)["spec"]["ports"])
    assert service_ports["grpc-tls"] == {
        "name": "grpc-tls",
        "port": 19443,
        "targetPort": "grpc-tls",
    }

    policy = _noded_policy(documents)
    tls_rows = [
        row
        for row in policy["spec"]["ingress"]
        if any(
            port.get("port") == "19443"
            for to_ports in row.get("toPorts", [])
            for port in to_ports.get("ports", [])
        )
    ]
    assert len(tls_rows) == 1
    selectors = [source["matchLabels"] for source in tls_rows[0]["fromEndpoints"]]
    assert selectors == [
        {
            "k8s:io.kubernetes.pod.namespace": release,
            "app.kubernetes.io/name": "embervm",
            "app.kubernetes.io/instance": release,
        }
    ]


def test_explicit_allowlist_and_tls_only_mode_remove_plaintext_exposure() -> None:
    documents = _render(
        "tls-only",
        [
            "noded.plaintextGrpc.enabled=false",
            "noded.spiffe.enabled=true",
            "noded.networkPolicy.enabled=true",
            "noded.spiffe.clientSpiffeIds={spiffe://custom.test/control-a,spiffe://custom.test/control-b}",
        ],
    )
    for pod in _noded_pods(documents):
        container = _noded_container(pod)
        env = _named(container["env"])
        assert env["EMBERVM_NODED_PLAINTEXT_GRPC_ENABLED"]["value"] == "false"
        assert "EMBERVM_NODED_LISTEN_ADDR" not in env
        assert env["EMBERVM_NODED_SPIFFE_CLIENT_IDS"]["value"] == (
            "spiffe://custom.test/control-a,spiffe://custom.test/control-b"
        )
        assert set(_named(container["ports"])) >= {"grpc-tls", "health", "activator"}
        assert "grpc" not in _named(container["ports"])

    assert set(_named(_service(documents)["spec"]["ports"])) == {"grpc-tls"}
    policy_ports = {
        port["port"]
        for row in _noded_policy(documents)["spec"]["ingress"]
        for to_ports in row.get("toPorts", [])
        for port in to_ports.get("ports", [])
    }
    assert "9443" in policy_ports
    assert "9090" not in policy_ports


def test_render_rejects_disabling_both_grpc_listeners() -> None:
    with pytest.raises(RuntimeError, match="at least one of noded.plaintextGrpc"):
        _render(
            "no-listener",
            [
                "noded.plaintextGrpc.enabled=false",
                "noded.spiffe.enabled=false",
            ],
        )
