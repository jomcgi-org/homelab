"""Pin Grimoire's private data boundary: peers, keys, logins and grants."""

import base64
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess

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
# This account's R2 endpoint, where the hub writes the migration runs.
R2_HOST = "7c56b458cd657d96b095c63d181c051f.r2.cloudflarestorage.com"
R2 = (f"fqdn:{R2_HOST}", "443")
R2_SECRET = "grimoire-migration-r2"
# Every egress peer of each Job and runtime identity, besides DNS. No
# inference endpoint is configured yet.
PEERS = {
    "grimoire-api": {DATABASE, GARAGE},
    "grimoire-worker": {DATABASE, GARAGE},
    "grimoire-restore": {DATABASE, RECOVERED, R2},
    "grimoire-migration-check": {R2},
    "grimoire-db-setup": {("kube-apiserver", "6443")},
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


APPLICATION_ROOT = "projects/platform-factory/grimoire-data/resources"
DEPLOY = "projects/grimoire/deploy"


def materialize(tmp_path_factory):
    """Copy the Application's source tree out of the runfiles."""
    # Kustomize's load restriction rejects symlinks that resolve outside a
    # resource directory in a Bazel sandbox.
    root = tmp_path_factory.mktemp("kustomize")
    for directory in (APPLICATION_ROOT, DEPLOY):
        shutil.copytree(REPO / directory, root / directory)
    return root


def kustomize_build(root):
    return subprocess.run(
        [
            os.environ.get("KUSTOMIZE_BIN", "kustomize"),
            "build",
            root / APPLICATION_ROOT,
        ],
        capture_output=True,
        text=True,
    )


def add_resource(kustomization, entry):
    doc = yaml.safe_load(kustomization.read_text())
    doc["resources"].append(entry)
    kustomization.write_text(yaml.safe_dump(doc))


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """The grimoire Application's manifests, as Argo CD builds them."""
    result = kustomize_build(materialize(tmp_path_factory))
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def wave(doc):
    return int(
        doc["metadata"].get("annotations", {}).get("argocd.argoproj.io/sync-wave", "0")
    )


def peers(rule):
    ports = [port["port"] for entry in rule["toPorts"] for port in entry["ports"]]
    assert len(ports) == 1, rule
    if "toEntities" in rule:
        return {(entity, ports[0]) for entity in rule["toEntities"]}
    if "toFQDNs" in rule:
        # Exact names only: a pattern would reach other hosts.
        assert all(set(name) == {"matchName"} for name in rule["toFQDNs"]), rule
        return {(f"fqdn:{name['matchName']}", ports[0]) for name in rule["toFQDNs"]}
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
    for word in ("inference", "world", "toCIDR", "0.0.0.0"):
        assert word not in specs
    # The R2 endpoint is the only host outside the cluster, for two identities.
    outside = {
        name: {
            reached
            for reached in PEERS[name]
            if isinstance(reached[0], str) and reached[0].startswith("fqdn:")
        }
        for name in PEERS
    }
    assert {name for name, hosts in outside.items() if hosts} == {
        "grimoire-restore",
        "grimoire-migration-check",
    }
    assert set().union(*outside.values()) == {R2}


def test_runtime_and_restore_pods_select_their_policies(rendered):
    cronjob = by_kind(rendered, "CronJob")["grimoire-restore"]
    pod = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "grimoire-restore"
    assert pod["automountServiceAccountToken"] is False
    env = {item["name"]: item.get("value") for item in pod["containers"][0]["env"]}
    assert env["PGHOST"] == "grimoire-rw.grimoire.svc"
    assert env["PGUSER"] == "grimoire_restore"
    accounts = by_kind(resources("runtime.yaml"), "ServiceAccount")
    assert set(accounts) == {
        "grimoire-api",
        "grimoire-worker",
        "grimoire-restore",
        "grimoire-migration-check",
    }
    for account in accounts.values():
        assert account["automountServiceAccountToken"] is False


def on_demand_pod(rendered, name):
    cronjob = by_kind(rendered, "CronJob")[name]
    # The CronJob never starts a scheduled run or renders a Job of that name.
    assert cronjob["spec"]["suspend"] is True
    assert cronjob["spec"]["schedule"] == "0 0 31 2 *"
    assert name not in by_kind(rendered, "Job")
    job = cronjob["spec"]["jobTemplate"]["spec"]
    assert job["backoffLimit"] == 0
    pod = job["template"]["spec"]
    assert pod["serviceAccountName"] == name
    assert pod["automountServiceAccountToken"] is False
    return pod


def env_of(container):
    return {item["name"]: item for item in container["env"]}


def secrets_of(container):
    return {
        item["valueFrom"]["secretKeyRef"]["name"]
        for item in container["env"]
        if "secretKeyRef" in item.get("valueFrom", {})
    }


def reads_r2(container):
    env = env_of(container)
    assert env["RCLONE_CONFIG_R2_ENDPOINT"]["value"] == f"https://{R2_HOST}"
    assert env["R2_BUCKET"]["value"] == "grimoire"
    assert env["MIGRATION_PREFIX"]["value"] == "migration/"
    for name, key in (
        ("RCLONE_CONFIG_R2_ACCESS_KEY_ID", "access-key-id"),
        ("RCLONE_CONFIG_R2_SECRET_ACCESS_KEY", "secret-access-key"),
    ):
        assert env[name]["valueFrom"]["secretKeyRef"] == {"name": R2_SECRET, "key": key}
    assert container["image"].startswith("docker.io/rclone/rclone@sha256:")


def test_application_deploys_the_restore_on_demand_only(rendered):
    pod = on_demand_pod(rendered, "grimoire-restore")
    # An unbound claim stays Progressing, and Argo CD would then hold the
    # PostSync grants. Each run makes its own claim instead.
    assert "PersistentVolumeClaim" not in {doc["kind"] for doc in rendered}
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    claim = volumes["snapshot"]["ephemeral"]["volumeClaimTemplate"]["spec"]
    assert claim["storageClassName"] == "local-path"
    # Only the fetch container writes the dump and holds the R2 key. The
    # restore reads the dump and holds the database login.
    (fetch,) = pod["initContainers"]
    (restore,) = pod["containers"]
    mounts = {
        container["name"]: {mount["name"]: mount for mount in container["volumeMounts"]}
        for container in (fetch, restore)
    }
    assert not mounts["fetch"]["snapshot"].get("readOnly")
    assert mounts["pg-restore"]["snapshot"]["readOnly"] is True
    assert fetch["command"] == [
        "/bin/sh",
        "/scripts/migration.sh",
        "fetch",
        "/snapshot",
    ]
    reads_r2(fetch)
    assert secrets_of(fetch) == {R2_SECRET}
    assert secrets_of(restore) == {"grimoire-app"}
    assert restore["command"] == ["bash", "/scripts/restore.sh"]
    env = env_of(restore)
    assert env["PRIVATE_DUMP"]["value"] == "/snapshot/grimoire.dump"
    assert env["VECTOR_METADATA"]["value"] == "/snapshot/vector.metadata"
    assert env["SOURCE_INVENTORY"]["value"] == "/snapshot/source.inventory"
    assert env["INVENTORY_SQL"]["value"] == "/scripts/inventory.sql"
    assert mounts["pg-restore"]["scripts"]["mountPath"] == "/scripts"

    # The mounted scripts are the repository's.
    name = volumes["scripts"]["configMap"]["name"]
    script = by_kind(rendered, "ConfigMap")[name]
    assert name.startswith("grimoire-restore-script-")
    assert script["metadata"]["namespace"] == "grimoire"
    assert script["data"] == {
        file: (REPO / DEPLOY / file).read_text()
        for file in ("restore.sh", "migration.sh", "inventory.sql")
    }


def test_the_migration_check_reads_r2_and_nothing_else(rendered):
    pod = on_demand_pod(rendered, "grimoire-migration-check")
    assert "initContainers" not in pod
    (check,) = pod["containers"]
    assert check["command"] == ["/bin/sh", "/scripts/migration.sh", "check"]
    reads_r2(check)
    assert secrets_of(check) == {R2_SECRET}
    assert not any(name.startswith("PG") for name in env_of(check))
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    assert volumes["scripts"]["configMap"]["name"].startswith(
        "grimoire-restore-script-"
    )


def test_one_time_migration_check_reuses_the_read_only_template(rendered):
    cronjob = by_kind(rendered, "CronJob")["grimoire-migration-check"]
    job = by_kind(rendered, "Job")["grimoire-migration-check-home-746"]
    assert job["spec"]["template"] == cronjob["spec"]["jobTemplate"]["spec"]["template"]
    assert job["spec"]["backoffLimit"] == 0
    assert "ttlSecondsAfterFinished" not in job["spec"]


def test_the_r2_key_comes_from_its_1password_item(rendered):
    item = by_kind(rendered, "OnePasswordItem")[R2_SECRET]
    assert item["metadata"]["namespace"] == "grimoire"
    assert item["spec"] == {
        "itemPath": "vaults/k8s-homelab/items/grimoire-migration-r2"
    }
    # Only the restore and checksum workloads name the key.
    readers = set()
    for doc in rendered:
        if doc["kind"] in {"CronJob", "Job", "Deployment", "StatefulSet"}:
            if R2_SECRET in yaml.safe_dump(doc):
                readers.add(doc["metadata"]["name"])
    assert readers == {
        "grimoire-restore",
        "grimoire-migration-check",
        "grimoire-migration-check-home-746",
    }


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
    recovery = read(f"{DEPLOY}/recovery.yaml")[0]["spec"]
    source = recovery["externalClusters"][0]["barmanObjectStore"]
    assert recovery["bootstrap"]["recovery"]["source"] == source["serverName"]
    for key in ("destinationPath", "serverName", "endpointURL"):
        assert source[key] == store[key]
    assert source["s3Credentials"]["accessKeyId"]["name"] == "grimoire-recovery-s3"
    assert "backup" not in recovery


def test_application_deploys_recovery_from_the_runbook_step(tmp_path_factory):
    # README.md "Restore a backup", step 3: the child kustomization lists it.
    root = materialize(tmp_path_factory)
    add_resource(root / DEPLOY / "kustomization.yaml", "recovery.yaml")
    result = kustomize_build(root)
    assert result.returncode == 0, result.stderr
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    clusters = by_kind(docs, "Cluster")
    assert set(clusters) == {"grimoire", "grimoire-recovered"}
    assert clusters["grimoire-recovered"] == read(f"{DEPLOY}/recovery.yaml")[0]


def test_application_root_cannot_list_recovery_directly(tmp_path_factory):
    # The load restriction keeps files outside the Application's root out.
    root = materialize(tmp_path_factory)
    add_resource(
        root / APPLICATION_ROOT / "kustomization.yaml",
        "../../../grimoire/deploy/recovery.yaml",
    )
    result = kustomize_build(root)
    assert result.returncode != 0
    assert "is not in or below" in result.stderr


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
