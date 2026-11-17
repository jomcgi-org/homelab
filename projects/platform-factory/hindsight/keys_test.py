"""Exercise Hindsight's key Jobs against their Secret and RBAC manifests."""

import base64
import copy
import importlib.util
from pathlib import Path
from unittest.mock import Mock, mock_open, patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parent
KEYS = ("hindsight", "hindsight-keys")
AGENT_TOKEN = ("factory-r-homelab", "hindsight-mcp")


def read(path):
    return [doc for doc in yaml.safe_load_all((ROOT / path).read_text()) if doc]


def decode(value):
    return base64.b64decode(value).decode()


class SecretAPI:
    """Serve manifest placeholders, enforcing the Jobs' bound Roles."""

    def __init__(self):
        documents = read("resources/secrets.yaml") + read("agents/secrets.yaml")
        subject = {
            "kind": "ServiceAccount",
            "name": "hindsight-keys",
            "namespace": "hindsight",
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
        "hindsight_keys", ROOT / "resources/keys.py"
    )
    script = importlib.util.module_from_spec(spec)
    with (
        patch("builtins.open", mock_open(read_data="fake-service-account-token")),
        patch("ssl.create_default_context"),
    ):
        spec.loader.exec_module(script)
    api = SecretAPI()
    monkeypatch.setattr(script, "call", api.call)
    values = iter(["a" * 64, "b" * 64])
    monkeypatch.setattr(
        script.secrets, "token_hex", Mock(side_effect=lambda _: next(values))
    )
    return script, api


def test_init_fills_both_keys_once_and_never_rotates(keys, capsys):
    script, api = keys
    script.main("init")
    data = api.secrets[KEYS]["data"]
    assert decode(data["tenant-api-key"]) == "a" * 64
    assert decode(data["mcp-token"]) == "b" * 64
    assert api.writes == [KEYS]
    assert api.secrets[AGENT_TOKEN]["data"] == {}

    api.writes.clear()
    script.main("init")
    script.main("agents")
    assert api.writes == [AGENT_TOKEN]
    api.writes.clear()
    script.main("agents")
    assert api.writes == []
    assert script.secrets.token_hex.call_count == 2
    output = capsys.readouterr().out
    assert "a" * 64 not in output and "b" * 64 not in output


def test_agents_get_only_the_mcp_token(keys):
    script, api = keys
    script.main("agents")
    shared = api.secrets[AGENT_TOKEN]["data"]
    assert shared == {"token": api.secrets[KEYS]["data"]["mcp-token"]}
    assert shared["token"] != api.secrets[KEYS]["data"]["tenant-api-key"]


def test_agents_wait_for_the_other_application(keys, monkeypatch):
    script, api = keys
    placeholder = api.secrets.pop(AGENT_TOKEN)
    sleep = Mock(side_effect=lambda _: api.secrets.update({AGENT_TOKEN: placeholder}))
    monkeypatch.setattr(script.time, "sleep", sleep)
    script.main("agents")
    sleep.assert_called_once_with(10)
    assert set(api.secrets[AGENT_TOKEN]["data"]) == {"token"}


def test_agent_token_is_injectable_and_never_in_the_pod_as_the_tenant_key():
    (secret,) = [d for d in read("agents/secrets.yaml") if d["kind"] == "Secret"]
    assert secret["metadata"]["labels"] == {"substrate.ate.dev/injectable": "true"}
    (deployment,) = [
        d for d in read("resources/hindsight.yaml") if d["kind"] == "Deployment"
    ]
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e.get("value", e.get("valueFrom")) for e in container["env"]}
    ref = lambda name: env[name]["secretKeyRef"]  # noqa: E731
    assert ref("HINDSIGHT_API_TENANT_API_KEY") == {
        "name": "hindsight-keys",
        "key": "tenant-api-key",
    }
    assert ref("HINDSIGHT_API_MCP_AUTH_TOKEN") == {
        "name": "hindsight-keys",
        "key": "mcp-token",
    }
    # The agents' token reaches MCP only, and MCP registers only these.
    assert env["HINDSIGHT_API_MCP_ENABLED_TOOLS"] == "recall,retain"
    assert env["HF_HUB_OFFLINE"] == "1"
    assert deployment["spec"]["template"]["spec"]["enableServiceLinks"] is False


def test_only_the_egress_gateway_reaches_hindsight():
    policy = next(
        d
        for d in read("resources/network-policy.yaml")
        if d["metadata"]["name"] == "hindsight"
    )
    (rule,) = policy["spec"]["ingress"]
    assert rule["fromEndpoints"] == [
        {
            "matchLabels": {
                "k8s:io.kubernetes.pod.namespace": "ate-system",
                "app": "atenet-egress",
            }
        }
    ]
    fqdns = [f for r in policy["spec"]["egress"] for f in r.get("toFQDNs", [])]
    assert fqdns == [{"matchName": "api.anthropic.com"}]


def test_boundary_test_runs_the_deployed_image():
    # boundary_test runs inside this image (BUILD exec_properties); an image
    # bump that skipped it would leave the new version's boundary untested.
    (deployment,) = [
        d for d in read("resources/hindsight.yaml") if d["kind"] == "Deployment"
    ]
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    repository = container["image"].split(":", 1)[0]
    digest = container["image"].rsplit("@", 1)[1]
    build = (ROOT / "BUILD").read_text()
    assert f'"test.container-image": "docker://{repository}@{digest}"' in build


def test_the_script_configmap_name_is_not_a_role_resource_name():
    # Kustomize renames a generated ConfigMap with a hash suffix and rewrites
    # every Role resourceName that matches it, Secrets included. A shared name
    # left the Jobs without access to hindsight-keys, waiting forever.
    (kustomization,) = read("resources/kustomization.yaml")
    (generator,) = kustomization["configMapGenerator"]
    documents = read("resources/secrets.yaml") + read("agents/secrets.yaml")
    names = {
        name
        for doc in documents
        if doc["kind"] == "Role"
        for rule in doc["rules"]
        for name in rule.get("resourceNames", [])
    }
    assert generator["name"] not in names
    volumes = [
        volume["configMap"]["name"]
        for doc in documents
        if doc["kind"] == "Job"
        for volume in doc["spec"]["template"]["spec"]["volumes"]
    ]
    assert volumes == [generator["name"]] * 2
