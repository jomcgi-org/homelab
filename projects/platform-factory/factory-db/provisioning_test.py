"""Exercise factory-db's initializer using its Secret and RBAC manifests."""

import base64
import copy
import importlib.util
from pathlib import Path
from unittest.mock import Mock, mock_open, patch
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

ROOT = Path(__file__).resolve().parent
# (login Secret, database, client namespace, connection-string Secret there)
LOGINS = [
    ("factory-db-itsaplan-login", "itsaplan", "factory", "factory-itsaplan-db"),
    (
        "factory-db-connector-login",
        "factory_connector",
        "factory",
        "factory-connector-db",
    ),
    ("factory-db-reviewer-login", "factory_reviewer", "factory", "factory-reviewer-db"),
    ("factory-db-hindsight-login", "factory_hindsight", "hindsight", "hindsight-db"),
]
REVIEWER_LOGIN = ("factory-db", "factory-db-reviewer-login")
REVIEWER_CONNECTION = ("factory", "factory-reviewer-db")
HINDSIGHT_LOGIN = ("factory-db", "factory-db-hindsight-login")
HINDSIGHT_CONNECTION = ("hindsight", "hindsight-db")
# Each client's placeholders and the Role that lets factory-db-setup fill them.
CLIENT_SECRETS = ["factory/secrets.yaml", "../hindsight/resources/secrets.yaml"]


def read(path):
    return list(yaml.safe_load_all((ROOT / path).read_text()))


def encode(value):
    return base64.b64encode(value.encode()).decode()


class SecretAPI:
    """Serve manifest placeholders, enforcing the setup account's bound Roles."""

    def __init__(self):
        documents = read("resources/setup.yaml")
        for path in CLIENT_SECRETS:
            documents += read(path)
        self.secrets = {}
        self.permissions = set()
        self.writes = []
        subject = {
            "kind": "ServiceAccount",
            "name": "factory-db-setup",
            "namespace": "factory-db",
        }
        bound_roles = {
            (doc["metadata"]["namespace"], doc["roleRef"]["name"])
            for doc in documents
            if doc["kind"] == "RoleBinding"
            and doc["roleRef"]["kind"] == "Role"
            and subject in doc["subjects"]
        }
        for doc in documents:
            key = (doc["metadata"]["namespace"], doc["metadata"]["name"])
            if doc["kind"] == "Secret":
                secret = copy.deepcopy(doc)
                secret["data"] = {
                    name: encode(value)
                    for name, value in secret.pop("stringData", {}).items()
                }
                self.secrets[key] = secret
            elif doc["kind"] == "Role" and key in bound_roles:
                for rule in doc["rules"]:
                    assert rule["apiGroups"] == [""]
                    assert rule["resources"] == ["secrets"]
                    for name in rule["resourceNames"]:
                        for verb in rule["verbs"]:
                            self.permissions.add((key[0], name, verb))
        self.secrets[("factory-db", "factory-db-s3")]["data"] = {
            "AWS_SECRET_ACCESS_KEY": encode("fake-backup-key")
        }

    def call(self, method, path, body=None):
        key = (path.split("/")[4], path.rsplit("/", 1)[1])
        verb = {"GET": "get", "PUT": "update"}[method]
        assert (*key, verb) in self.permissions, f"RBAC denies {verb} {key}"
        if method == "GET":
            return copy.deepcopy(self.secrets.get(key))
        assert key in self.secrets, "ArgoCD must create the placeholder first"
        assert (body["metadata"]["namespace"], body["metadata"]["name"]) == key
        self.secrets[key] = copy.deepcopy(body)
        self.writes.append(key)
        return copy.deepcopy(body)


@pytest.fixture
def provisioning(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "kubernetes")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    spec = importlib.util.spec_from_file_location(
        "factory_db_secret_init", ROOT / "resources/secret_init.py"
    )
    script = importlib.util.module_from_spec(spec)
    with (
        patch("builtins.open", mock_open(read_data="fake-service-account-token")),
        patch("ssl.create_default_context"),
    ):
        spec.loader.exec_module(script)
    api = SecretAPI()
    monkeypatch.setattr(script, "call", api.call)
    monkeypatch.setattr(script.secrets, "token_hex", Mock(return_value="c" * 48))
    return script, api


@pytest.mark.parametrize("existing", [False, True], ids=["fresh", "add-later-logins"])
def test_provisioning_preserves_passwords_and_reruns_without_writes(
    provisioning, existing, capsys
):
    script, api = provisioning
    old_passwords = {}
    if existing:
        for index, (name, login, namespace, target) in enumerate(LOGINS[:2]):
            old_passwords[name] = str(index + 1) * 48
            api.secrets[("factory-db", name)]["data"]["password"] = encode(
                old_passwords[name]
            )
            url = (
                f"postgres://{login}:{old_passwords[name]}@"
                f"factory-db-rw.factory-db.svc:5432/{login}?sslmode=require"
            )
            api.secrets[(namespace, target)]["data"] = {"DATABASE_URL": encode(url)}

    script.main("init")
    assert set(api.writes) == (
        {REVIEWER_LOGIN, HINDSIGHT_LOGIN}
        if existing
        else {("factory-db", name) for name, _, _, _ in LOGINS}
    )
    script.secrets.token_hex.assert_called_with(24)
    assert script.secrets.token_hex.call_count == (2 if existing else 4)
    passwords = {}
    for name, login, _, _ in LOGINS:
        data = api.secrets[("factory-db", name)]["data"]
        assert base64.b64decode(data["username"]).decode() == login
        passwords[name] = base64.b64decode(data["password"]).decode()
        assert passwords[name] == old_passwords.get(name, "c" * 48)
    assert "DATABASE_URL" not in api.secrets[REVIEWER_CONNECTION]["data"]
    assert "DATABASE_URL" not in api.secrets[HINDSIGHT_CONNECTION]["data"]

    api.writes.clear()
    script.main("connections")
    assert set(api.writes) == (
        {REVIEWER_CONNECTION, HINDSIGHT_CONNECTION}
        if existing
        else {(namespace, target) for _, _, namespace, target in LOGINS}
    )
    for name, login, namespace, target in LOGINS:
        data = api.secrets[(namespace, target)]["data"]
        assert set(data) == {"DATABASE_URL"}
        url = urlsplit(base64.b64decode(data["DATABASE_URL"]).decode())
        assert url.scheme == "postgres"
        assert url.username == login
        assert url.password == passwords[name]
        assert url.hostname == "factory-db-rw.factory-db.svc"
        assert url.port == 5432
        assert url.path == f"/{login}"
        assert parse_qs(url.query) == {"sslmode": ["require"]}

    snapshot = copy.deepcopy(api.secrets)
    api.writes.clear()
    script.main("init")
    script.main("connections")
    assert api.secrets == snapshot
    assert api.writes == []
    assert script.secrets.token_hex.call_count == (2 if existing else 4)
    output = capsys.readouterr().out
    assert all(password not in output for password in passwords.values())


def test_connections_wait_for_the_other_application(provisioning, monkeypatch):
    script, api = provisioning
    placeholder = api.secrets.pop(REVIEWER_CONNECTION)
    sleep = Mock(
        side_effect=lambda _: api.secrets.update({REVIEWER_CONNECTION: placeholder})
    )
    monkeypatch.setattr(script.time, "sleep", sleep)
    script.main("connections")
    sleep.assert_called_once_with(10)
    assert set(api.secrets[REVIEWER_CONNECTION]["data"]) == {"DATABASE_URL"}


def test_connections_repair_a_stale_url_without_rotating_login(provisioning):
    script, api = provisioning
    script.main("connections")
    login = copy.deepcopy(api.secrets[REVIEWER_LOGIN])
    api.secrets[REVIEWER_CONNECTION]["data"] = {"DATABASE_URL": encode("stale")}
    api.writes.clear()
    script.main("connections")
    assert api.secrets[REVIEWER_LOGIN] == login
    assert api.writes == [REVIEWER_CONNECTION]
    assert script.secrets.token_hex.call_count == 4


def test_managed_roles_and_databases_use_the_provisioned_logins():
    documents = read("resources/cluster.yaml")
    cluster = next(doc for doc in documents if doc["kind"] == "Cluster")
    roles = {role["name"]: role for role in cluster["spec"]["managed"]["roles"]}
    for secret, login, _, _ in LOGINS:
        assert roles[login]["login"] is True
        assert roles[login]["ensure"] == "present"
        assert roles[login]["passwordSecret"]["name"] == secret
    databases = {
        doc["spec"]["name"]: doc for doc in documents if doc["kind"] == "Database"
    }
    for _, login, _, _ in LOGINS[1:]:
        database = databases[login]
        assert database["spec"]["owner"] == login
        assert database["spec"]["cluster"]["name"] == cluster["metadata"]["name"]
        assert database["spec"]["ensure"] == "present"
        assert database["spec"]["databaseReclaimPolicy"] == "retain"
        assert database["metadata"]["namespace"] == "factory-db"
        annotations = database["metadata"]["annotations"]
        assert annotations["argocd.argoproj.io/sync-wave"] == "1"
        assert annotations["argocd.argoproj.io/sync-options"] == (
            "Prune=false,Delete=false"
        )
    # Hindsight's migrations need both in public, and vector needs a superuser.
    assert databases["factory_hindsight"]["spec"]["extensions"] == [
        {"name": name, "ensure": "present", "schema": "public"}
        for name in ("vector", "pg_trgm")
    ]


@pytest.mark.parametrize(
    "namespace,name,port,allowed",
    [
        ("factory", "factory-reviewer", "5432", True),
        ("factory", "factory-connector", "5432", True),
        ("hindsight", "hindsight", "5432", True),
        ("hindsight", "hindsight-keys", "5432", False),
        ("other", "factory-reviewer", "5432", False),
        ("factory", "unrelated", "5432", False),
        ("factory", "factory-reviewer", "8000", False),
    ],
)
def test_database_ingress_is_scoped(namespace, name, port, allowed):
    policy = read("resources/network-policy.yaml")[0]
    labels = {
        "k8s:io.kubernetes.pod.namespace": namespace,
        "app.kubernetes.io/name": name,
    }
    assert policy["metadata"]["namespace"] == "factory-db"
    assert policy["spec"]["endpointSelector"]["matchLabels"] == {
        "cnpg.io/cluster": "factory-db"
    }
    permitted = any(
        any(
            endpoint["matchLabels"].items() <= labels.items()
            for endpoint in rule["fromEndpoints"]
        )
        and any(
            entry == {"port": port, "protocol": "TCP"}
            for group in rule["toPorts"]
            for entry in group["ports"]
        )
        for rule in policy["spec"]["ingress"]
    )
    assert permitted is allowed
