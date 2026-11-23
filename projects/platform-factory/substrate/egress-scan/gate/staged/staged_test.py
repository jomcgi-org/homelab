"""Validate the staged engine-gate manifests: they render, stay off, give the
gate the identity it needs and the scanner none, and open only the flows that
the design names."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent
_IMAGE = "ghcr.io/jomcgi/homelab/projects/platform-factory/substrate/egress-scan/gate"


def _docs() -> list[dict]:
    out = subprocess.run(
        [os.environ["KUSTOMIZE_BIN"], "build", str(_HERE)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _one(kind: str, name: str) -> dict:
    (found,) = [
        d for d in _docs() if d["kind"] == kind and d["metadata"]["name"] == name
    ]
    return found


def _pod() -> dict:
    return _one("StatefulSet", "engine-gate")["spec"]["template"]["spec"]


def test_statefulset_has_no_replicas() -> None:
    sts = _one("StatefulSet", "engine-gate")
    assert sts["spec"]["replicas"] == 0
    assert sts["spec"]["serviceName"] == "engine-gate"
    assert sts["metadata"]["namespace"] == "ate-system"


def test_image_is_ours_and_not_pinned_by_hand() -> None:
    image = _pod()["containers"][0]["image"]
    assert image == f"{_IMAGE}:unpinned"
    assert "@sha256" not in image


def test_pod_is_locked_down() -> None:
    pod = _pod()
    assert pod["automountServiceAccountToken"] is False
    assert pod["serviceAccountName"] == "engine-gate"
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["runAsUser"] == 65532
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    (c,) = pod["containers"]
    sc = c["securityContext"]
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["readOnlyRootFilesystem"] is True
    assert sc["capabilities"]["drop"] == ["ALL"]
    assert not pod.get("hostNetwork") and not pod.get("hostPID")
    assert c["resources"]["requests"] == {"cpu": "100m", "memory": "128Mi"}
    assert "memory" in c["resources"]["limits"]


def test_no_secret_in_the_environment() -> None:
    (c,) = _pod()["containers"]
    assert [e["name"] for e in c["env"]] == ["POD_NAME"]
    assert "envFrom" not in c
    for v in _pod()["volumes"]:
        assert "secret" not in v and "configMap" not in v
    for arg in c["args"]:
        lowered = arg.lower()
        assert not any(w in lowered for w in ("token=", "secret", "password", "key="))


def test_identity_comes_from_projected_volumes() -> None:
    volumes = {v["name"]: v["projected"]["sources"][0] for v in _pod()["volumes"]}
    token = volumes["ate-token"]["serviceAccountToken"]
    assert token["audience"] == "api.ate-system.svc"
    assert token["expirationSeconds"] == 3600
    cert = volumes["router"]["podCertificate"]
    assert cert["signerName"] == "podidentity.podcert.ate.dev/identity"
    ca = volumes["router-ca"]["clusterTrustBundle"]
    assert ca["signerName"] == "servicedns.podcert.ate.dev/identity"
    mounts = {
        m["name"]: m["mountPath"] for m in _pod()["containers"][0]["volumeMounts"]
    }
    assert mounts == {
        "ate-token": "/var/run/ate",
        "router": "/var/run/router",
        "router-ca": "/var/run/router-ca",
    }
    assert all(m.get("readOnly") for m in _pod()["containers"][0]["volumeMounts"])


def test_service_exposes_only_the_scan_port() -> None:
    svc = _one("Service", "engine-gate")
    assert [(p["name"], p["port"]) for p in svc["spec"]["ports"]] == [("scan", 8080)]
    assert svc["spec"]["selector"] == {"app": "engine-gate"}


def test_scan_port_is_open_to_the_scanner_only() -> None:
    policy = _one("CiliumNetworkPolicy", "engine-gate")["spec"]
    assert policy["endpointSelector"]["matchLabels"] == {"app": "engine-gate"}
    (rule,) = policy["ingress"]
    assert rule["fromEndpoints"] == [{"matchLabels": {"app": "egress-scan"}}]
    assert rule["toPorts"] == [{"ports": [{"port": "8080", "protocol": "TCP"}]}]


def test_gate_egress_is_the_api_and_the_router_only() -> None:
    policy = _one("CiliumNetworkPolicy", "engine-gate")["spec"]
    reached = sorted(
        (r["toEndpoints"][0]["matchLabels"]["app"], p["port"])
        for r in policy["egress"]
        for p in r["toPorts"][0]["ports"]
    )
    # DNS is open to every pod by the cluster-wide default-deny.
    assert reached == [("ate-api-server", "443"), ("atenet-router", "8443")]
    for rule in policy["egress"]:
        assert len(rule) == 2  # no toEntities, toCIDR or toFQDNs
        assert len(rule["toEndpoints"]) == 1


def test_api_server_admits_the_gate_on_443() -> None:
    policy = _one("CiliumNetworkPolicy", "ate-api-server-engine-gate")["spec"]
    assert policy["endpointSelector"]["matchLabels"] == {"app": "ate-api-server"}
    (rule,) = policy["ingress"]
    assert rule["fromEndpoints"] == [{"matchLabels": {"app": "engine-gate"}}]
    assert rule["toPorts"][0]["ports"] == [{"port": "443", "protocol": "TCP"}]


def test_scanner_gets_one_egress_rule_and_no_grant() -> None:
    docs = _docs()
    policy = _one("CiliumNetworkPolicy", "egress-scan-to-engine-gate")["spec"]
    (rule,) = policy["egress"]
    assert rule["toEndpoints"] == [{"matchLabels": {"app": "engine-gate"}}]
    assert rule["toPorts"][0]["ports"] == [{"port": "8080", "protocol": "TCP"}]
    # The scanner gets no ServiceAccount, no binding and no router rule here.
    assert not [
        d
        for d in docs
        if d["kind"] in ("ServiceAccount", "Role", "RoleBinding", "ClusterRoleBinding")
        and "egress-scan" in d["metadata"]["name"]
    ]


def test_prometheus_reaches_only_the_admin_port() -> None:
    policy = _one("CiliumClusterwideNetworkPolicy", "prometheus-scrape-engine-gate")[
        "spec"
    ]
    (rule,) = policy["ingress"]
    assert rule["toPorts"][0]["ports"] == [{"port": "9090", "protocol": "TCP"}]


def test_nothing_is_cluster_scoped_except_the_scrape_rule() -> None:
    kinds = {
        (d["kind"], d["metadata"]["name"])
        for d in _docs()
        if "namespace" not in d["metadata"]
    }
    assert kinds == {
        ("CiliumClusterwideNetworkPolicy", "prometheus-scrape-engine-gate")
    }
