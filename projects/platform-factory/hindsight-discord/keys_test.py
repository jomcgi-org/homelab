"""Exercise Hindsight's key Jobs against their Secret and RBAC manifests."""

import copy
import importlib.util
from pathlib import Path
from unittest.mock import Mock, mock_open, patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parent
KEYS = ("hindsight-discord", "hindsight-keys")
BRIDGE_KEY = ("factory-discord", "hindsight-discord-key")


def read(path):
    return [doc for doc in yaml.safe_load_all((ROOT / path).read_text()) if doc]


class SecretAPI:
    """Serve manifest placeholders, enforcing the Jobs' bound Roles."""

    def __init__(self):
        documents = read("resources/secrets.yaml") + read("discord/secrets.yaml")
        subject = {
            "kind": "ServiceAccount",
            "name": "hindsight-keys",
            "namespace": "hindsight-discord",
        }
        bound = {
            (doc["metadata"]["namespace"], doc["roleRef"]["name"])
            for doc in documents
            if doc["kind"] == "RoleBinding" and subject in doc["subjects"]
        }
        self.secrets, self.permissions, self.writes = {}, set(), []
        for doc in documents:
            key = (doc["metadata"]["namespace"], doc["metadata"]["name"])
            if doc["kind"] == "Secret":
                self.secrets[key] = {**copy.deepcopy(doc), "data": {}}
            elif doc["kind"] == "Role" and key in bound:
                for rule in doc["rules"]:
                    for name in rule["resourceNames"]:
                        for verb in rule["verbs"]:
                            self.permissions.add((key[0], name, verb))

    def call(self, method, path, body=None):
        key = (path.split("/")[4], path.rsplit("/", 1)[1])
        verb = {"GET": "get", "PUT": "update"}[method]
        if (*key, verb) not in self.permissions:
            # The real API answers 403, which keys.py reads as missing.
            assert method == "GET", f"RBAC denies {verb} {key}"
            return None
        if method == "GET":
            return copy.deepcopy(self.secrets.get(key))
        assert key in self.secrets, "ArgoCD must create the placeholder first"
        self.secrets[key] = copy.deepcopy(body)
        self.writes.append(key)
        return copy.deepcopy(body)


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "kubernetes")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT", "443")
    spec = importlib.util.spec_from_file_location(
        "hindsight_discord_keys", ROOT / "resources/keys.py"
    )
    script = importlib.util.module_from_spec(spec)
    with (
        patch("builtins.open", mock_open(read_data="fake-service-account-token")),
        patch("ssl.create_default_context"),
    ):
        spec.loader.exec_module(script)
    api = SecretAPI()
    monkeypatch.setattr(script, "call", api.call)
    values = iter(["a" * 64])
    monkeypatch.setattr(
        script.secrets, "token_hex", Mock(side_effect=lambda _: next(values))
    )
    return script, api


def test_key_is_generated_once_and_shared_only_with_discord(keys, capsys):
    script, api = keys
    script.main("init")
    data = api.secrets[KEYS]["data"]
    assert data == {"tenant-api-key": script.b64("a" * 64)}
    assert api.writes == [KEYS]
    assert api.secrets[BRIDGE_KEY]["data"] == {}

    script.main("discord")
    assert api.secrets[BRIDGE_KEY]["data"] == data
    assert api.writes == [KEYS, BRIDGE_KEY]
    api.writes.clear()
    script.main("init")
    script.main("discord")
    assert api.writes == []
    script.secrets.token_hex.assert_called_once_with(32)
    assert "a" * 64 not in capsys.readouterr().out
    assert api.permissions == {
        (*key, verb) for key in (KEYS, BRIDGE_KEY) for verb in ("get", "update")
    }


def test_consumer_waits_for_its_application_and_repairs_a_stale_copy(keys, monkeypatch):
    script, api = keys
    placeholder = api.secrets.pop(BRIDGE_KEY)
    sleep = Mock(side_effect=lambda _: api.secrets.update({BRIDGE_KEY: placeholder}))
    monkeypatch.setattr(script.time, "sleep", sleep)
    script.main("discord")
    sleep.assert_called_once_with(10)
    api.secrets[BRIDGE_KEY]["data"] = {"tenant-api-key": script.b64("stale")}
    api.writes.clear()
    script.main("discord")
    assert api.writes == [BRIDGE_KEY]
    assert api.secrets[BRIDGE_KEY]["data"] == api.secrets[KEYS]["data"]
    script.secrets.token_hex.assert_called_once_with(32)


def test_only_the_dedicated_llm_and_tenant_credentials_reach_the_api():
    deployment = next(
        d for d in read("resources/hindsight.yaml") if d["kind"] == "Deployment"
    )
    pod = deployment["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    env = {e["name"]: e.get("value", e.get("valueFrom")) for e in container["env"]}
    assert env["HINDSIGHT_API_LLM_PROVIDER"] == "openai"
    assert env["HINDSIGHT_API_LLM_MODEL"] == "deepseek/deepseek-v4.1-flash-1017"
    assert env["HINDSIGHT_API_MCP_ENABLED"] == "false"
    assert "HINDSIGHT_API_MCP_AUTH_TOKEN" not in env
    assert "HINDSIGHT_API_SKIP_LLM_VERIFICATION" not in env
    for variable, secret, key in (
        ("LLM_API_KEY", "hindsight-openrouter", "api-key"),
        ("LLM_BASE_URL", "hindsight-openrouter", "base-url"),
        ("TENANT_API_KEY", "hindsight-keys", "tenant-api-key"),
        ("DATABASE_URL", "hindsight-db", "DATABASE_URL"),
    ):
        assert env[f"HINDSIGHT_API_{variable}"] == {
            "secretKeyRef": {"name": secret, "key": key}
        }
    item = next(
        d for d in read("resources/secrets.yaml") if d["kind"] == "OnePasswordItem"
    )
    assert item["metadata"]["name"] == "hindsight-openrouter"
    assert item["spec"]["itemPath"] == "vaults/k8s-homelab/items/factory-discord-llm"
    assert env["HF_HUB_OFFLINE"] == env["TRANSFORMERS_OFFLINE"] == "1"
    assert deployment["spec"]["replicas"] == 1
    assert pod["automountServiceAccountToken"] is False
    assert pod["enableServiceLinks"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert container["securityContext"]["allowPrivilegeEscalation"] is False


def test_network_allows_only_discord_database_and_openrouter():
    policy, key_policy = read("resources/network-policy.yaml")
    assert policy["metadata"]["namespace"] == "hindsight-discord"
    assert policy["spec"]["endpointSelector"] == {
        "matchLabels": {"app.kubernetes.io/name": "hindsight"}
    }
    assert policy["spec"]["ingress"] == [
        {
            "fromEndpoints": [
                {
                    "matchLabels": {
                        "k8s:io.kubernetes.pod.namespace": "factory-discord",
                    }
                }
            ],
            "toPorts": [{"ports": [{"port": "8888", "protocol": "TCP"}]}],
        }
    ]
    assert policy["spec"]["egress"] == [
        {
            "toEndpoints": [
                {
                    "matchLabels": {
                        "k8s:io.kubernetes.pod.namespace": "factory-db",
                        "cnpg.io/cluster": "factory-db",
                    }
                }
            ],
            "toPorts": [{"ports": [{"port": "5432", "protocol": "TCP"}]}],
        },
        {
            "toFQDNs": [{"matchName": "openrouter.ai"}],
            "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}],
        },
    ]
    assert key_policy["spec"]["endpointSelector"] == {
        "matchLabels": {"app.kubernetes.io/name": "hindsight-keys"}
    }
    assert key_policy["spec"]["egress"] == [{"toEntities": ["kube-apiserver"]}]
    (client,) = read("discord/network-policy.yaml")
    assert client["metadata"]["namespace"] == "factory-discord"
    assert client["spec"] == {
        "endpointSelector": {},
        "egress": [
            {
                "toEndpoints": [
                    {
                        "matchLabels": {
                            "k8s:io.kubernetes.pod.namespace": "hindsight-discord",
                            "app.kubernetes.io/name": "hindsight",
                        }
                    }
                ],
                "toPorts": [{"ports": [{"port": "8888", "protocol": "TCP"}]}],
            }
        ],
    }


def test_argo_preserves_keys_and_jobs_use_the_tested_modes():
    for path, namespace, names in (
        ("application.yaml", "hindsight-discord", {"hindsight-keys", "hindsight-db"}),
        ("application-discord.yaml", "factory-discord", {"hindsight-discord-key"}),
    ):
        (application,) = read(path)
        assert application["spec"]["destination"]["namespace"] == namespace
        assert (
            "RespectIgnoreDifferences=true"
            in application["spec"]["syncPolicy"]["syncOptions"]
        )
        assert {d["name"] for d in application["spec"]["ignoreDifferences"]} == names
        assert all(
            d["jsonPointers"] == ["/data"]
            for d in application["spec"]["ignoreDifferences"]
        )
    docs = read("resources/secrets.yaml") + read("discord/secrets.yaml")
    for doc in docs:
        if doc["kind"] == "Secret":
            assert (
                doc["metadata"]["annotations"]["argocd.argoproj.io/sync-options"]
                == "Prune=false,Delete=false"
            )
            assert (
                not doc["metadata"]
                .get("labels", {})
                .get("substrate.ate.dev/injectable")
            )
    jobs = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Job"}
    for name, mode in (
        ("hindsight-keys-init", "init"),
        ("hindsight-discord-key", "discord"),
    ):
        pod = jobs[name]["spec"]["template"]["spec"]
        assert pod["serviceAccountName"] == "hindsight-keys"
        assert pod["containers"][0]["command"] == [
            "python3",
            "-I",
            "-u",
            "/scripts/keys.py",
            mode,
        ]


def test_boundary_test_runs_the_deployed_image():
    deployment = next(
        d for d in read("resources/hindsight.yaml") if d["kind"] == "Deployment"
    )
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    repository = container["image"].split(":", 1)[0]
    digest = container["image"].rsplit("@", 1)[1]
    assert (
        f'"test.container-image": "docker://{repository}@{digest}"'
        in (ROOT / "BUILD").read_text()
    )


def test_discord_manifests_have_no_factory_llm_references():
    for directory in ("resources", "discord"):
        for path in (ROOT / directory).glob("*.yaml"):
            text = path.read_text()
            assert all(
                name not in text
                for name in (
                    "factory-hindsight",
                    "hindsight-anthropic",
                    "api.anthropic.com",
                )
            ), path
