"""Exercise the inactive recovery policy's allowed flows and isolation boundary.

This evaluates only the selector/ipBlock/port subset used by this manifest.
It is a static contract test, not a substitute for GKE dataplane probes.
"""

from ipaddress import ip_address, ip_network
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent / "dev" / "deploy"
MONOLITH = "monolith-dev"
EMBER = "embervm-dev"


def _app(name, instance, component=None):
    labels = {
        "app.kubernetes.io/name": name,
        "app.kubernetes.io/instance": instance,
    }
    if component:
        labels["app.kubernetes.io/component"] = component
    return labels


PODS = {
    "backend": (MONOLITH, _app("monolith", MONOLITH, "app")),
    "control": (EMBER, _app("embervm", EMBER)),
    "broker": (EMBER, _app("embervm-tokenbroker", EMBER, "tokenbroker")),
    "noded": (EMBER, _app("embervm-noded", EMBER, "noded-brick")),
    "postgres": (MONOLITH, {"cnpg.io/cluster": "monolith-dev-pg"}),
    "operator": ("cnpg-system", _app("cloudnative-pg", "cnpg")),
    "dns": ("kube-system", {"k8s-app": "kube-dns"}),
}


@pytest.fixture(scope="module")
def policies():
    return list(yaml.safe_load_all((ROOT / "recovery-network-policy.yaml").read_text()))


def _matches(selector, labels):
    assert set(selector) <= {"matchLabels"}
    return all(labels.get(k) == v for k, v in selector.get("matchLabels", {}).items())


def _peer_matches(peer, other, address):
    if "ipBlock" in peer:
        assert set(peer) == {"ipBlock"}
        # Dataplane V2 ipBlock rules do not match pod destinations.
        if other is not None:
            return False
        block = peer["ipBlock"]
        ip = ip_address(address)
        return ip in ip_network(block["cidr"]) and not any(
            ip in ip_network(cidr) for cidr in block.get("except", [])
        )
    assert set(peer) == {"namespaceSelector", "podSelector"}
    if other is None:
        return False
    return _matches(
        peer["namespaceSelector"], {"kubernetes.io/metadata.name": other[0]}
    ) and _matches(peer["podSelector"], other[1])


def _allows(policies, direction, subject, other, port, protocol="TCP", address=None):
    selected = [
        p
        for p in policies
        if p["metadata"]["namespace"] == subject[0]
        and direction.title() in p["spec"]["policyTypes"]
        and _matches(p["spec"]["podSelector"], subject[1])
    ]
    if not selected:
        return True
    peer_key = "to" if direction == "egress" else "from"
    for policy in selected:
        for rule in policy["spec"][direction]:
            assert set(rule) == {peer_key, "ports"}
            if {"protocol": protocol, "port": port} in rule["ports"] and any(
                _peer_matches(peer, other, address) for peer in rule[peer_key]
            ):
                return True
    return False


def test_default_deny_covers_unknown_pods_in_both_namespaces(policies):
    assert {p["metadata"]["namespace"] for p in policies} == {MONOLITH, EMBER}
    for namespace in (MONOLITH, EMBER):
        for direction in ("ingress", "egress"):
            assert not _allows(policies, direction, (namespace, {}), PODS["dns"], 53)
            assert not _allows(
                policies, direction, (namespace, {}), None, 443, address="8.8.8.8"
            )


@pytest.mark.parametrize(
    "source,target,ports",
    [
        ("backend", "postgres", [5432]),
        ("backend", "control", [8080]),
        ("backend", "broker", [8080]),
        ("control", "postgres", [5432]),
        ("control", "noded", [9090]),
        ("noded", "control", [8080]),
        ("noded", "broker", [8080]),
        ("noded", "backend", [8000, 8091]),
        ("operator", "postgres", [8000, 5432]),
        ("postgres", "postgres", [5432]),
    ],
)
def test_required_flows_allow_both_ends(policies, source, target, ports):
    for port in ports:
        assert _allows(policies, "egress", PODS[source], PODS[target], port)
        assert _allows(policies, "ingress", PODS[target], PODS[source], port)


def test_dns_and_private_api_are_separate_grants(policies):
    for role in ("backend", "control", "broker", "noded", "postgres"):
        for protocol in ("TCP", "UDP"):
            assert _allows(policies, "egress", PODS[role], PODS["dns"], 53, protocol)
            assert _allows(
                policies, "egress", PODS[role], None, 53, protocol, "10.10.16.10"
            )
        assert _allows(
            policies, "egress", PODS[role], None, 443, address="10.10.0.2"
        ) == (role in {"control", "broker", "postgres"})


def test_public_https_does_not_grant_private_addresses_or_pod_access(policies):
    for role in ("backend", "control", "broker", "noded", "postgres"):
        assert _allows(
            policies, "egress", PODS[role], None, 443, address="8.8.8.8"
        ) == (role != "postgres")
        assert not _allows(policies, "egress", PODS[role], None, 80, address="8.8.8.8")
        for address in ("10.10.0.3", "169.254.169.254", "192.168.1.1", "100.64.0.1"):
            assert not _allows(
                policies, "egress", PODS[role], None, 443, address=address
            )
        for namespace, labels in PODS.values():
            assert not _allows(policies, "egress", PODS[role], (namespace, labels), 443)


def test_namespaces_and_complete_pod_identity_are_required(policies):
    for role in ("backend", "control", "broker", "noded", "postgres"):
        for peer_namespace, peer_labels in PODS.values():
            impostors = [("production", peer_labels), (peer_namespace, {})]
            impostors += [
                (peer_namespace, {**peer_labels, key: "wrong"}) for key in peer_labels
            ]
            for impostor in impostors:
                for port in (53, 443, 5432, 8000, 8080, 8091, 9090):
                    for direction in ("ingress", "egress"):
                        assert not _allows(
                            policies, direction, PODS[role], impostor, port
                        )


def test_backend_cannot_manage_database_or_call_guest_directly(policies):
    assert not _allows(policies, "ingress", PODS["postgres"], PODS["backend"], 8000)
    for port in (8080, 8081, 9090, 5400, 5410):
        assert not _allows(policies, "ingress", PODS["noded"], PODS["backend"], port)
    for port in (3000, 8081, 9090):
        assert not _allows(policies, "ingress", PODS["backend"], PODS["noded"], port)


def test_secret_resources_join_recovery_consumers_without_values():
    items = list(yaml.safe_load_all((ROOT / "recovery-secrets.yaml").read_text()))
    values = yaml.safe_load((ROOT / "values-recovery-gke.yaml").read_text())
    assert len(items) == 2
    for item in items:
        assert set(item) == {"apiVersion", "kind", "metadata", "type", "spec"}
        assert item["apiVersion"] == "onepassword.com/v1"
        assert item["kind"] == "OnePasswordItem"
        assert set(item["spec"]) == {"itemPath"}
    database, identity = items
    assert database["metadata"] == {
        "namespace": MONOLITH,
        "name": values["postgres"]["embervmOpLog"]["passwordSecret"],
        "labels": {"cnpg.io/reload": ""},
    }
    assert database["type"] == "kubernetes.io/basic-auth"
    assert database["spec"]["itemPath"] == (
        "vaults/k8s-homelab/items/embervm-recovery-oplog-db"
    )
    assert identity["metadata"] == {
        "namespace": EMBER,
        "name": "embervm-authentik-recovery-agent",
    }
    assert identity["type"] == "Opaque"
    assert identity["spec"]["itemPath"] == (
        "vaults/k8s-homelab/items/embervm-authentik-recovery-agent"
    )
