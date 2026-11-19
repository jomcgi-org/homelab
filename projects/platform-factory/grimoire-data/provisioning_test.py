"""Pin Grimoire's private data boundary: peers, keys, logins and grants."""

import base64
import hashlib
import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
DNS = {
    "toEndpoints": [
        {
            "matchLabels": {
                "k8s:io.kubernetes.pod.namespace": "kube-system",
                "k8s:k8s-app": "kube-dns",
            }
        }
    ],
    "toPorts": [
        {
            "ports": [{"port": "53", "protocol": "ANY"}],
            "rules": {"dns": [{"matchPattern": "*"}]},
        }
    ],
}


def peer(labels, port):
    return frozenset(labels.items()), port


DATABASE = peer({"cnpg.io/cluster": "grimoire"}, "5432")
RECOVERED = peer({"cnpg.io/cluster": "grimoire-recovered"}, "5432")
GARAGE = peer(
    {"k8s:io.kubernetes.pod.namespace": "garage", "app.kubernetes.io/name": "garage"},
    "3900",
)
# Every egress peer of each Job and runtime identity, besides DNS. No
# inference endpoint is configured yet.
PEERS = {
    "grimoire-api": {DATABASE, GARAGE},
    "grimoire-worker": {DATABASE, GARAGE},
    "grimoire-restore": {DATABASE, RECOVERED},
    "grimoire-db-setup": {("kube-apiserver", None)},
}
# (Secret, bucket, access)
CLAIMS = {
    ("grimoire-backup-s3", "grimoire-backups", "readwrite"),
    ("grimoire-recovery-s3", "grimoire-backups", "read"),
    ("grimoire-api-s3", "grimoire-private", "read"),
    ("grimoire-worker-s3", "grimoire-private", "readwrite"),
}


def read(path):
    return [doc for doc in yaml.safe_load_all((REPO / path).read_text()) if doc]


def resources(name):
    return read(f"projects/platform-factory/grimoire-data/resources/{name}")


def by_kind(docs, kind):
    return {doc["metadata"]["name"]: doc for doc in docs if doc["kind"] == kind}


def wave(doc):
    return int(
        doc["metadata"].get("annotations", {}).get("argocd.argoproj.io/sync-wave", "0")
    )


def peers(rule):
    if "toEntities" in rule:
        assert "toPorts" not in rule
        return {(entity, None) for entity in rule["toEntities"]}
    ports = [port["port"] for entry in rule["toPorts"] for port in entry["ports"]]
    assert len(ports) == 1, rule
    return {peer(selector["matchLabels"], ports[0]) for selector in rule["toEndpoints"]}


@pytest.mark.parametrize("name", sorted(PEERS))
def test_each_identity_reaches_dns_and_only_its_peers(name):
    policy = by_kind(resources("policy.yaml"), "CiliumNetworkPolicy")[name]
    assert "ingress" not in policy["spec"]
    egress = policy["spec"]["egress"]
    assert egress.count(DNS) == 1
    reached = set()
    for rule in egress:
        if rule != DNS:
            reached |= peers(rule)
    assert reached == PEERS[name]


def test_no_policy_reaches_inference_or_the_world():
    specs = yaml.safe_dump([doc["spec"] for doc in resources("policy.yaml")])
    for word in ("inference", "world", "toCIDR", "toFQDNs", "0.0.0.0"):
        assert word not in specs


def test_runtime_and_restore_pods_select_their_policies():
    job = read("projects/grimoire/deploy/restore-job.yaml")[0]
    pod = job["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "grimoire-restore"
    assert pod["automountServiceAccountToken"] is False
    env = {item["name"]: item.get("value") for item in pod["containers"][0]["env"]}
    assert env["PGHOST"] == "grimoire-rw.grimoire.svc"
    assert env["PGUSER"] == "grimoire_restore"
    accounts = by_kind(resources("runtime.yaml"), "ServiceAccount")
    assert set(accounts) == {"grimoire-api", "grimoire-worker", "grimoire-restore"}
    for account in accounts.values():
        assert account["automountServiceAccountToken"] is False


def test_database_admits_only_grimoire_identities():
    cluster = by_kind(resources("policy.yaml"), "CiliumNetworkPolicy")["grimoire"]
    clients = cluster["spec"]["ingress"][0]["fromEndpoints"]
    accounts = {
        selector["matchLabels"].get("io.cilium.k8s.policy.serviceaccount")
        for selector in clients
    }
    assert accounts == {"grimoire-api", "grimoire-worker", "grimoire-restore", None}


def test_private_buckets_have_scoped_keys_and_no_public_mirror():
    storage = resources("storage.yaml")
    claims = by_kind(storage, "ConfigMap")
    found = set()
    for name, claim in claims.items():
        data = claim["data"]
        assert set(data) == {"namespace", "secretName", "bucket", "access"}
        assert data["namespace"] == "grimoire"
        digest = hashlib.sha256(f"grimoire/{data['secretName']}".encode()).hexdigest()
        assert name == f"garage-claim-{digest}"
        assert wave(claim) <= -2
        found.add((data["secretName"], data["bucket"], data["access"]))
    assert found == CLAIMS
    secrets = by_kind(storage, "Secret")
    assert set(secrets) == {secret for secret, _, _ in CLAIMS}
    role = by_kind(storage, "Role")["garage-init"]
    assert set(role["rules"][0]["resourceNames"]) == set(secrets)


def test_garage_admits_grimoire_runtime_and_databases_only():
    policy = read("projects/platform-factory/garage/resources/network-policy.yaml")[0]
    s3 = next(
        rule
        for rule in policy["spec"]["ingress"]
        if rule["toPorts"][0]["ports"][0]["port"] == "3900"
    )
    grimoire = {
        frozenset(selector["matchLabels"].items())
        for selector in s3["fromEndpoints"]
        if selector.get("matchLabels", {}).get("k8s:io.kubernetes.pod.namespace")
        == "grimoire"
    }
    expected = [
        {"io.cilium.k8s.policy.serviceaccount": "grimoire-api"},
        {"io.cilium.k8s.policy.serviceaccount": "grimoire-worker"},
        {"cnpg.io/cluster": "grimoire"},
        {"cnpg.io/cluster": "grimoire-recovered"},
    ]
    assert grimoire == {
        frozenset({**labels, "k8s:io.kubernetes.pod.namespace": "grimoire"}.items())
        for labels in expected
    }


def test_logins_come_from_the_chart():
    setup = resources("setup.yaml")
    cluster = by_kind(resources("database.yaml"), "Cluster")["grimoire"]
    roles = cluster["spec"]["managed"]["roles"]
    placeholders = by_kind(setup, "Secret")
    assert {role["name"]: role["passwordSecret"]["name"] for role in roles} == {
        "grimoire_api": "grimoire-api-login",
        "grimoire_worker": "grimoire-worker-login",
    }
    for role in roles:
        assert role["login"] is True
        for attribute in (
            "superuser",
            "createdb",
            "createrole",
            "replication",
            "bypassrls",
        ):
            assert role[attribute] is False
        secret = placeholders[role["passwordSecret"]["name"]]
        assert secret["type"] == "kubernetes.io/basic-auth"
        assert secret["stringData"] == {"username": role["name"]}
    assert cluster["spec"]["bootstrap"]["initdb"]["owner"] == "grimoire_restore"
    assert cluster["spec"]["enableSuperuserAccess"] is False

    jobs = by_kind(setup, "Job")
    assert wave(jobs["grimoire-db-init"]) < wave(cluster)
    backup = by_kind(resources("storage.yaml"), "Secret")["grimoire-backup-s3"]
    assert wave(backup) < wave(jobs["grimoire-db-init"])
    grants = jobs["grimoire-db-grants"]
    assert grants["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "PostSync"
    pod = grants["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "grimoire-restore"
    container = pod["containers"][0]
    assert "--file=/scripts/grants.sql" in container["command"]
    env = {item["name"]: item.get("value") for item in container["env"]}
    assert env["PGUSER"] == "grimoire_restore"


def test_backups_are_scheduled_and_restorable():
    docs = resources("database.yaml")
    store = by_kind(docs, "Cluster")["grimoire"]["spec"]["backup"]["barmanObjectStore"]
    scheduled = by_kind(docs, "ScheduledBackup")["grimoire-daily"]["spec"]
    assert scheduled["cluster"]["name"] == "grimoire"
    recovery = read("projects/grimoire/deploy/recovery.yaml")[0]["spec"]
    source = recovery["externalClusters"][0]["barmanObjectStore"]
    assert recovery["bootstrap"]["recovery"]["source"] == source["serverName"]
    for key in ("destinationPath", "serverName", "endpointURL"):
        assert source[key] == store[key]
    assert source["s3Credentials"]["accessKeyId"]["name"] == "grimoire-recovery-s3"
    assert "backup" not in recovery


class FakeAPI:
    def __init__(self, secrets, key_after=0):
        self.secrets = secrets
        self.key_after = key_after
        self.puts = []

    def __call__(self, method, path, body=None):
        name = path.rsplit("/", 1)[1]
        assert path == f"/api/v1/namespaces/grimoire/secrets/{name}"
        if method == "PUT":
            self.puts.append(name)
            self.secrets[name] = body
            return body
        assert method == "GET"
        if name == "grimoire-backup-s3":
            if self.key_after:
                self.key_after -= 1
                return {"data": {}}
            return {"data": {"AWS_SECRET_ACCESS_KEY": "a2V5"}}
        return self.secrets[name]


def load_init(monkeypatch, api):
    path = ROOT / "resources/secret_init.py"
    spec = importlib.util.spec_from_file_location("grimoire_secret_init", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "call", api)
    return module


def decode(secret, key):
    return base64.b64decode(secret["data"][key]).decode()


def test_init_fills_missing_passwords_once(monkeypatch):
    kept = base64.b64encode(b"existing").decode()
    api = FakeAPI(
        {
            "grimoire-api-login": {"data": {}},
            "grimoire-worker-login": {
                "data": {
                    "username": base64.b64encode(b"grimoire_worker").decode(),
                    "password": kept,
                }
            },
        },
        key_after=2,
    )
    init = load_init(monkeypatch, api)
    init.ensure_logins()
    init.wait_for_key(sleep=lambda _: None)
    assert api.puts == ["grimoire-api-login"]
    created = api.secrets["grimoire-api-login"]
    assert decode(created, "username") == "grimoire_api"
    assert len(decode(created, "password")) == 48
    assert api.secrets["grimoire-worker-login"]["data"]["password"] == kept
    assert api.key_after == 0

    init.ensure_logins()
    assert api.puts == ["grimoire-api-login"]
