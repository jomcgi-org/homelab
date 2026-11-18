"""Check both ends of backup/replication flows and the scoped Garage key."""

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
NS = "k8s:io.kubernetes.pod.namespace"
PG = {NS: "authentik", "cnpg.io/cluster": "authentik-pg"}
GARAGE = {NS: "garage", "app.kubernetes.io/name": "garage"}


def read(path):
    return [doc for doc in yaml.safe_load_all((ROOT / path).read_text()) if doc]


POLICIES = read("authentik/resources/network-policy.yaml") + read(
    "garage/resources/network-policy.yaml"
)


def selects(selector, labels, namespace):
    expected = {NS: namespace, **selector["matchLabels"]}
    return expected.items() <= labels.items()


def allows(source, destination, port, direction):
    """Evaluate the endpoint/port allowlists used by these namespaced policies."""
    local, peer = (
        (source, destination) if direction == "egress" else (destination, source)
    )
    endpoint_key = "toEndpoints" if direction == "egress" else "fromEndpoints"
    for policy in POLICIES:
        namespace = policy["metadata"]["namespace"]
        spec = policy["spec"]
        if not selects(spec["endpointSelector"], local, namespace):
            continue
        for rule in spec.get(direction, []):
            if any(
                selects(selector, peer, namespace)
                for selector in rule.get(endpoint_key, [])
            ) and any(
                entry == {"port": str(port), "protocol": "TCP"}
                for group in rule.get("toPorts", [])
                for entry in group["ports"]
            ):
                return True
    return False


@pytest.mark.parametrize("port,destination", [(5432, PG), (8000, PG), (3900, GARAGE)])
def test_replication_and_backups_have_both_policy_directions(port, destination):
    assert allows(PG, destination, port, "egress")
    assert allows(PG, destination, port, "ingress")


@pytest.mark.parametrize("component", ["server", "worker"])
def test_application_pods_keep_only_database_access(component):
    app = {
        NS: "authentik",
        "app.kubernetes.io/name": "authentik",
        "app.kubernetes.io/component": component,
    }
    assert allows(app, PG, 5432, "egress")
    assert allows(app, PG, 5432, "ingress")
    assert not allows(app, GARAGE, 3900, "egress")
    assert not allows(app, GARAGE, 3900, "ingress")


def test_backup_access_stays_scoped():
    assert not allows(PG, GARAGE, 3903, "egress")
    assert not allows(PG, GARAGE, 3903, "ingress")
    foreign_pg = {**PG, NS: "factory"}
    assert not allows(foreign_pg, GARAGE, 3900, "ingress")
    foreign_store = {**GARAGE, NS: "factory"}
    assert not allows(PG, foreign_store, 3900, "egress")


def test_backup_key_is_scoped_and_preserved():
    spec = importlib.util.spec_from_file_location(
        "garage_init", ROOT / "garage/resources/garage_init.py"
    )
    garage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(garage)
    assert garage.REGION == "garage"
    assert "authentik-pg-backups" in garage.BUCKETS
    assert [grant for grant in garage.GRANTS if grant[0] == "authentik-pg-backups"] == [
        ("authentik-pg-backups", "authentik", "authentik-pg-s3")
    ]
    docs = {
        doc["kind"]: doc for doc in read("factory-buckets/resources/authentik.yaml")
    }
    secret = docs["Secret"]
    assert secret["metadata"]["name"] == "authentik-pg-s3"
    assert secret["metadata"]["namespace"] == "authentik"
    assert secret["metadata"]["annotations"]["argocd.argoproj.io/sync-options"] == (
        "Prune=false,Delete=false"
    )
    assert "data" not in secret and "stringData" not in secret
    assert docs["Role"]["metadata"]["namespace"] == "authentik"
    assert docs["Role"]["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "resourceNames": ["authentik-pg-s3"],
            "verbs": ["get", "update"],
        }
    ]
    binding = docs["RoleBinding"]
    assert binding["metadata"]["namespace"] == "authentik"
    assert binding["roleRef"]["name"] == docs["Role"]["metadata"]["name"]
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "garage-init", "namespace": "garage"}
    ]
    assert (
        "authentik.yaml"
        in read("factory-buckets/resources/kustomization.yaml")[0]["resources"]
    )


def test_cluster_waits_for_all_backup_credentials():
    job = read("authentik/resources/backup-key-wait.yaml")[0]
    assert job["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "-1"
    assert job["spec"]["activeDeadlineSeconds"] == 1800
    pod = job["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    references = {
        env["name"]: env["valueFrom"]["secretKeyRef"]
        for env in pod["containers"][0]["env"]
    }
    assert references == {
        key: {"name": "authentik-pg-s3", "key": key}
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION")
    }
    assert (
        "backup-key-wait.yaml"
        in read("authentik/resources/kustomization.yaml")[0]["resources"]
    )


def test_authentik_external_egress_is_limited_to_provider_https():
    from pathlib import Path

    policies = yaml.safe_load_all(
        Path(__file__).with_name("network-policy.yaml").read_text()
    )
    expected = {
        "server": {
            "github.com",
            "api.github.com",
            "oauth2.googleapis.com",
            "www.googleapis.com",
            "discord.com",
        },
        "worker": {
            "accounts.google.com",
            "www.googleapis.com",
            "token.actions.githubusercontent.com",
        },
    }
    for policy in policies:
        name = policy["metadata"]["name"]
        if name not in expected:
            continue
        database, *external = sorted(
            policy["spec"]["egress"], key=lambda rule: "toFQDNs" in rule
        )
        assert database["toEndpoints"] == [
            {"matchLabels": {"cnpg.io/cluster": "authentik-pg"}}
        ]
        hosts = set()
        for rule in external:
            assert set(rule) == {"toFQDNs", "toPorts"}
            (peer,) = rule["toFQDNs"]
            (port_rule,) = rule["toPorts"]
            assert port_rule["ports"] == [{"port": "443", "protocol": "TCP"}]
            assert port_rule["serverNames"] == [peer["matchName"]]
            hosts.add(peer["matchName"])
        assert hosts == expected.pop(name)
    assert not expected
