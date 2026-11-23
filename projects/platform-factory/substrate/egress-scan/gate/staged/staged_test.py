"""Validate the staged engine-gate manifests: they render, stay off, give the
gate the identity it needs and the scanner none, and open only the flows that
the design names."""

from __future__ import annotations

import os
import re
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
    assert pod["securityContext"]["fsGroup"] == 65532
    # The same read login as the live egress-scan Deployment.
    assert pod["imagePullSecrets"] == [{"name": "ghcr-imagepull-secret"}]
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


def test_ports_are_named_as_the_policies_assume() -> None:
    # Cilium matches a port number. A rename of "admin" or "scan", or a change
    # of a number, must not open a port to a source that was not meant to have
    # it: the scan port belongs to the scanner and the admin port to Prometheus.
    (c,) = _pod()["containers"]
    ports = {p["name"]: p["containerPort"] for p in c["ports"]}
    assert ports == {"scan": 8080, "admin": 9090}
    assert c["readinessProbe"]["httpGet"]["port"] == "admin"
    assert c["livenessProbe"]["httpGet"]["port"] == "admin"
    scan_policy = _one("CiliumNetworkPolicy", "engine-gate")["spec"]["ingress"]
    scrape = _one("CiliumClusterwideNetworkPolicy", "prometheus-scrape-engine-gate")[
        "spec"
    ]["ingress"]
    allowed: dict[str, list[str]] = {}
    for rule in scan_policy + scrape:
        sources = [
            m["matchLabels"].get("app") or m["matchLabels"]["app.kubernetes.io/name"]
            for m in rule["fromEndpoints"]
        ]
        for p in rule["toPorts"][0]["ports"]:
            allowed.setdefault(p["port"], []).extend(sources)
    assert allowed == {"8080": ["egress-scan"], "9090": ["prometheus"]}
    # The Service reaches the scan port by name and exposes nothing else.
    (sp,) = _one("Service", "engine-gate")["spec"]["ports"]
    assert sp["targetPort"] == "scan" and ports["scan"] == int(sp["port"])


def test_only_argocd_may_run_a_pod_as_the_gate() -> None:
    exempt = {
        "system:serviceaccount:argocd:argocd-application-controller",
        "system:serviceaccount:kube-system:statefulset-controller",
        "system:serviceaccount:kube-system:generic-garbage-collector",
    }
    shapes = {
        "engine-gate-serviceaccount": {"serviceaccounts"},
        "engine-gate-pods": {"pods"},
        "engine-gate-workloads": {
            "statefulsets",
            "deployments",
            "replicasets",
            "daemonsets",
            "jobs",
        },
    }
    for name, resources in shapes.items():
        policy = _one("ValidatingAdmissionPolicy", name)["spec"]
        binding = _one("ValidatingAdmissionPolicyBinding", name)["spec"]
        assert policy["failurePolicy"] == "Fail"
        assert binding["policyName"] == name
        assert binding["validationActions"] == ["Deny"]
        selector = policy["matchConstraints"]["namespaceSelector"]["matchLabels"]
        assert selector == {"kubernetes.io/metadata.name": "ate-system"}
        covered = {r for rule in policy["matchConstraints"]["resourceRules"] for r in rule["resources"]}
        assert covered == resources
        for rule in policy["matchConstraints"]["resourceRules"]:
            assert set(rule["operations"]) == {"CREATE", "UPDATE"}
        (cond,) = policy["matchConditions"]
        # The exempt identities are exactly these and no others.
        assert set(re.findall(r"'(system:serviceaccount:[^']+)'", cond["expression"])) == exempt
        (check,) = policy["validations"]
        assert "engine-gate" in check["expression"]


def test_nothing_is_cluster_scoped_except_the_scrape_rule_and_the_guards() -> None:
    kinds = {
        (d["kind"], d["metadata"]["name"])
        for d in _docs()
        if "namespace" not in d["metadata"]
    }
    assert kinds == {
        ("CiliumClusterwideNetworkPolicy", "prometheus-scrape-engine-gate"),
        *{
            (kind, f"engine-gate-{shape}")
            for kind in ("ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding")
            for shape in ("serviceaccount", "pods", "workloads")
        },
    }
