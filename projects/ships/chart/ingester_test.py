import json
import os
from pathlib import Path
import subprocess

import yaml


def render(*overrides):
    result = subprocess.check_output(
        [
            os.getenv("HELM_BIN", "helm"),
            "template",
            "ships",
            str(Path(__file__).parent),
            "--set",
            "image.digest=sha256:" + "0" * 64,
            *overrides,
        ]
    )
    return [d for d in yaml.safe_load_all(result) if d]


def deployment(docs):
    return next(d["spec"] for d in docs if d["kind"] == "Deployment")


def test_activation():
    # HOME-537 runs one replica by default. enabled=false is the rollback.
    for overrides, replicas in (([], 1), (["--set", "enabled=false"], 0)):
        spec = deployment(render(*overrides))
        assert spec["replicas"] == replicas
        assert spec["strategy"]["type"] == "Recreate"
        container = spec["template"]["spec"]["containers"][0]
        assert container["env"][-1]["value"] == "false"


def test_cutover_defaults():
    # The starting budgets that ARCHITECTURE.md states, at one replica.
    docs = render()
    spec = deployment(docs)
    assert spec["replicas"] == 1
    container = spec["template"]["spec"]["containers"][0]
    assert container["resources"] == {
        "requests": {"cpu": "250m", "memory": "512Mi"},
        "limits": {"cpu": "1", "memory": "1Gi"},
    }
    cluster = next(d["spec"] for d in docs if d["kind"] == "Cluster")
    assert cluster["storage"] == {"storageClass": "local-path", "size": "20Gi"}
    assert cluster["resources"] == {
        "requests": {"cpu": "100m", "memory": "512Mi"},
        "limits": {"memory": "1Gi"},
    }
    # The hub reads the same 1Password item and key (monolith marine values).
    key = next(e for e in container["env"] if e["name"] == "AISSTREAM_API_KEY")
    ref = key["valueFrom"]["secretKeyRef"]
    assert ref["key"] == "ais-stream-api-key"
    item = next(
        d
        for d in docs
        if d["kind"] == "OnePasswordItem" and d["metadata"]["name"] == ref["name"]
    )
    assert item["spec"]["itemPath"] == "vaults/k8s-homelab/items/marine"
    policy = next(
        d["spec"]
        for d in docs
        if d["kind"] == "CiliumNetworkPolicy"
        and d["metadata"]["name"] == "ships-ingester"
    )
    assert {
        "toFQDNs": [{"matchName": "stream.aisstream.io"}],
        "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}],
    } in policy["egress"]


def test_sized_database():
    docs = render(
        "--set", "database.storage=5Gi,database.resources.requests.memory=128Mi"
    )
    cluster = next(d["spec"] for d in docs if d["kind"] == "Cluster")
    assert cluster["storage"]["size"] == "5Gi"
    (ref,) = cluster["bootstrap"]["initdb"]["postInitApplicationSQLRefs"][
        "configMapRefs"
    ]
    config = next(
        d
        for d in docs
        if d["kind"] == "ConfigMap" and d["metadata"]["name"] == ref["name"]
    )
    assert "CREATE TABLE ships.heat_cells_historical" in config["data"][ref["key"]]


def render_publisher(*overrides):
    result = subprocess.check_output(
        [
            os.getenv("HELM_BIN", "helm"),
            "template",
            "ships",
            str(Path(__file__).parent),
            "--namespace",
            "ships",
            "--set",
            "image.digest=sha256:" + "0" * 64,
            *overrides,
        ],
        text=True,
    )
    docs = [d for d in yaml.safe_load_all(result) if d]
    return result, {(d["kind"], d["metadata"]["name"]): d for d in docs}


def test_publisher_stays_suspended_until_activation():
    # HOME-410 sets publisher.suspend=false after HOME-537 turns the ingester on.
    for overrides, suspended in (
        ([], True),
        (["--set", "publisher.suspend=false"], False),
    ):
        _, docs = render_publisher(*overrides)
        job = docs["CronJob", "ships-publish"]["spec"]
        assert job["suspend"] is suspended
        assert job["schedule"] == "*/2 * * * *"
        assert job["concurrencyPolicy"] == "Forbid"


def test_publisher_writes_only_its_garage_bucket():
    rendered, docs = render_publisher()
    claims = {
        (d["data"]["secretName"], d["data"]["bucket"], d["data"]["access"])
        for (kind, _), d in docs.items()
        if kind == "ConfigMap" and d["metadata"].get("namespace") == "garage"
    }
    # The factory-public release mirrors the bucket to R2 under ships/
    # (HOME-492). This namespace holds no R2 credential and runs no mirror.
    assert claims == {("ships-ships-s3", "ships", "readwrite")}
    assert "R2_BUCKET" not in rendered
    assert "factory-r2-public-mirror" not in rendered
    pod = docs["CronJob", "ships-publish"]["spec"]["jobTemplate"]["spec"]["template"][
        "spec"
    ]
    container = pod["containers"][0]
    assert container["args"] == ["publish"]
    assert container["image"].endswith("@sha256:" + "0" * 64)
    assert {
        e["valueFrom"]["secretKeyRef"]["name"]
        for e in container["env"]
        if "valueFrom" in e
    } == {"ships-ships-s3", "ships-publisher-login"}
    assert pod["automountServiceAccountToken"] is False
    # Egress: Garage from the library policy, the database from the chart.
    library = docs["CiliumNetworkPolicy", "ships-publish"]["spec"]
    assert library["enableDefaultDeny"] == {"ingress": True, "egress": True}
    assert [rule["toPorts"][0]["ports"][0]["port"] for rule in library["egress"]] == [
        "3900"
    ]
    database = docs["CiliumNetworkPolicy", "ships-publish-db"]["spec"]
    assert database["endpointSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "ships-publish"
    }
    assert database["egress"][0]["toEndpoints"] == [
        {"matchLabels": {"cnpg.io/cluster": "ships-db"}}
    ]
    ingress = docs["CiliumNetworkPolicy", "ships-db"]["spec"]["ingress"][0]
    assert {"app.kubernetes.io/name": "ships-publish"} in [
        e["matchLabels"] for e in ingress["fromEndpoints"]
    ]


def test_publisher_never_gets_the_owner_credential():
    # The owner login can alter or drop the ingestion tables. The publisher
    # connects as the read-only ships_publisher login only.
    for overrides in ([], ["--set", "publisher.suspend=false"]):
        _, docs = render_publisher(*overrides)
        job = docs["CronJob", "ships-publish"]
        assert "ships-db-app" not in json.dumps(job)
        container = job["spec"]["jobTemplate"]["spec"]["template"]["spec"][
            "containers"
        ][0]
        env = {e["name"]: e for e in container["env"]}
        assert env["DATABASE_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
            "name": "ships-publisher-login",
            "key": "password",
        }
        assert env["DATABASE_URL"]["value"] == (
            "postgresql://ships_publisher:$(DATABASE_PASSWORD)"
            "@ships-db-rw.ships.svc:5432/ships?sslmode=require"
        )
        # Kubernetes expands $(DATABASE_PASSWORD) only from an earlier entry.
        names = [e["name"] for e in container["env"]]
        assert names.index("DATABASE_PASSWORD") < names.index("DATABASE_URL")
    # Only the ingester mounts the owner credential.
    rendered, docs = render_publisher()
    owners = {
        (kind, name)
        for (kind, name), doc in docs.items()
        if "ships-db-app" in json.dumps(doc)
    }
    assert owners == {("Deployment", "ships-ingester")}


def test_publisher_login_is_a_managed_read_only_role():
    _, docs = render_publisher()
    (role,) = docs["Cluster", "ships-db"]["spec"]["managed"]["roles"]
    assert role == {
        "name": "ships_publisher",
        "ensure": "present",
        "login": True,
        "inherit": False,
        "superuser": False,
        "createdb": False,
        "createrole": False,
        "replication": False,
        "bypassrls": False,
        "connectionLimit": 2,
        "passwordSecret": {"name": "ships-publisher-login"},
    }
    secret = docs["Secret", "ships-publisher-login"]
    assert secret["type"] == "kubernetes.io/basic-auth"
    assert secret["stringData"] == {"username": "ships_publisher"}
    assert "data" not in secret
    assert secret["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    # The generator runs before the Cluster and may only fill this Secret.
    job = docs["Job", "ships-publisher-login-generate"]
    assert job["metadata"]["annotations"]["argocd.argoproj.io/hook"] == "Sync"
    assert job["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "-1"
    container = job["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env["SECRET_NAME"] == "ships-publisher-login"
    assert json.loads(env["SECRET_KEYS"]) == {"password": 24}
    assert docs["Role", "ships-publisher-login-generate"]["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "resourceNames": ["ships-publisher-login"],
            "verbs": ["get", "patch"],
        }
    ]
    # ArgoCD keeps the generated password.
    application = yaml.safe_load(Path(os.environ["SHIPS_APPLICATION"]).read_text())[
        "spec"
    ]
    assert {
        "group": "",
        "kind": "Secret",
        "name": "ships-publisher-login",
        "jsonPointers": ["/data"],
    } in application["ignoreDifferences"]
    assert "RespectIgnoreDifferences=true" in application["syncPolicy"]["syncOptions"]


def test_publisher_login_waits_for_a_sized_database():
    # Without database.storage the chart renders no database, no login and
    # no generator Job.
    _, docs = render_publisher("--set", "database.storage=")
    assert not {k for k in docs if "ships-publisher-login" in k[1]}
    assert ("Cluster", "ships-db") not in docs
