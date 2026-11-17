"""Exercise Hindsight's key Jobs against their Secret and RBAC manifests."""

import base64
import copy
import importlib.util
import json
import re
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
    assert fqdns == [{"matchName": "api.anthropic.com"}, {"matchName": "openrouter.ai"}]


def test_embeddings_use_voyage_through_openrouter_without_data_retention():
    # HOME-176: the shared OpenRouter key (HOME-130 comment 1439), a model
    # whose 1,024 dimensions pgvector can index, and no data-keeping providers.
    (deployment,) = [
        d for d in read("resources/hindsight.yaml") if d["kind"] == "Deployment"
    ]
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e.get("value", e.get("valueFrom")) for e in container["env"]}
    assert env["HINDSIGHT_API_EMBEDDINGS_PROVIDER"] == "openrouter"
    assert env["HINDSIGHT_API_EMBEDDINGS_OPENROUTER_MODEL"] == "mongodb/voyage-4-large"
    assert env["HINDSIGHT_API_EMBEDDINGS_OPENROUTER_API_KEY"] == {
        "secretKeyRef": {"name": "hindsight-openrouter", "key": "api-key"}
    }
    assert json.loads(env["HINDSIGHT_API_EMBEDDINGS_OPENAI_EXTRA_BODY"]) == {
        "provider": {"data_collection": "deny"}
    }
    (item,) = [
        d
        for d in read("resources/secrets.yaml")
        if d["kind"] == "OnePasswordItem"
        and d["metadata"]["name"] == "hindsight-openrouter"
    ]
    assert item["spec"]["itemPath"] == "vaults/k8s-homelab/items/factory-discord-llm"


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
    images = re.findall(r'"test\.container-image": "([^"]+)"', build)
    # boundary_test and rollback_test both run inside the deployed image.
    assert images == [f"docker://{repository}@{digest}"] * 2


def test_the_script_configmap_names_are_not_role_resource_names():
    # Kustomize renames a generated ConfigMap with a hash suffix and rewrites
    # every Role resourceName that matches it, Secrets included. A shared name
    # left the Jobs without access to hindsight-keys, waiting forever.
    (kustomization,) = read("resources/kustomization.yaml")
    generators = {g["name"] for g in kustomization["configMapGenerator"]}
    assert generators == {"hindsight-keys-script", "hindsight-preflight-script"}
    documents = read("resources/secrets.yaml") + read("agents/secrets.yaml")
    names = {
        name
        for doc in documents
        if doc["kind"] == "Role"
        for rule in doc["rules"]
        for name in rule.get("resourceNames", [])
    }
    assert not generators & names
    volumes = [
        volume["configMap"]["name"]
        for doc in documents
        if doc["kind"] == "Job"
        for volume in doc["spec"]["template"]["spec"]["volumes"]
    ]
    assert volumes == ["hindsight-keys-script"] * 2


def manifest(path, kind, name=None):
    (doc,) = [
        d
        for d in read(path)
        if d["kind"] == kind and (name is None or d["metadata"]["name"] == name)
    ]
    return doc


def container_env(pod_spec):
    (container,) = pod_spec["containers"]
    env = {e["name"]: e.get("value", e.get("valueFrom")) for e in container["env"]}
    return container, env


def test_preflight_runs_after_the_secrets_and_before_the_deployment():
    job = manifest("resources/hindsight.yaml", "Job", "hindsight-preflight")
    annotations = job["metadata"]["annotations"]
    assert annotations["argocd.argoproj.io/hook"] == "Sync"
    assert annotations["argocd.argoproj.io/hook-delete-policy"] == "BeforeHookCreation"
    # After the 1Password items and placeholders (-2), before the Deployment (0).
    assert annotations["argocd.argoproj.io/sync-wave"] == "-1"
    for doc in read("resources/secrets.yaml"):
        if doc["kind"] in ("OnePasswordItem", "Secret"):
            wave = doc["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]
            assert wave == "-2"
    deployment = manifest("resources/hindsight.yaml", "Deployment")
    assert "argocd.argoproj.io/sync-wave" not in deployment["metadata"].get(
        "annotations", {}
    )
    # The hook's ServiceAccount must exist before the hook, on a fresh install.
    account = manifest("resources/hindsight.yaml", "ServiceAccount")
    assert account["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "-2"
    assert job["spec"]["template"]["spec"]["serviceAccountName"] == "hindsight"

    container, env = container_env(job["spec"]["template"]["spec"])
    api, api_env = container_env(deployment["spec"]["template"]["spec"])
    assert container["image"] == api["image"]
    assert container["command"][-1] == "/scripts/preflight.py"
    # The same Secrets and settings as the API pod, so a missing one stops here.
    assert env["EMBEDDINGS_DIMENSIONS"] == "1024"  # mongodb/voyage-4-large
    for name, api_name in (
        ("OPENROUTER_API_KEY", "HINDSIGHT_API_EMBEDDINGS_OPENROUTER_API_KEY"),
        ("DATABASE_URL", "HINDSIGHT_API_DATABASE_URL"),
        ("EMBEDDINGS_MODEL", "HINDSIGHT_API_EMBEDDINGS_OPENROUTER_MODEL"),
        ("EMBEDDINGS_EXTRA_BODY", "HINDSIGHT_API_EMBEDDINGS_OPENAI_EXTRA_BODY"),
    ):
        assert env[name] == api_env[api_name], name


def test_only_the_api_pod_serves_and_every_pod_can_reach_the_database():
    service = manifest("resources/hindsight.yaml", "Service")
    assert service["spec"]["selector"] == {
        "app.kubernetes.io/name": "hindsight",
        "app.kubernetes.io/component": "api",
    }
    pods = {
        "api": manifest("resources/hindsight.yaml", "Deployment"),
        "preflight": manifest("resources/hindsight.yaml", "Job", "hindsight-preflight"),
        "reembed": manifest("recovery/reembed-local.yaml", "Job"),
    }
    for component, doc in pods.items():
        # factory-db's policy admits app.kubernetes.io/name: hindsight.
        assert doc["spec"]["template"]["metadata"]["labels"] == {
            "app.kubernetes.io/name": "hindsight",
            "app.kubernetes.io/component": component,
        }


def load_preflight():
    spec = importlib.util.spec_from_file_location(
        "hindsight_preflight", ROOT / "resources/preflight.py"
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    return script


V384, V1024 = "vector(384)", "vector(1024)"


@pytest.mark.parametrize(
    "columns,dimension,ok",
    [
        ({}, 1024, True),  # A new database: Hindsight creates the tables.
        ({"memory_units": (V384, 0), "mental_models": (V384, 0)}, 1024, True),
        ({"memory_units": (V1024, 9), "mental_models": (V1024, 0)}, 1024, True),
        ({"memory_units": (V384, 9), "mental_models": (V384, 0)}, 1024, False),
        ({"memory_units": (V1024, 0), "mental_models": (V1024, 2)}, 384, False),
    ],
)
def test_preflight_refuses_only_rows_of_another_dimension(columns, dimension, ok):
    assert (load_preflight().refused(columns, dimension) == []) == ok


def test_preflight_stops_the_sync_on_a_bad_key_model_or_stored_rows(monkeypatch):
    script = load_preflight()
    monkeypatch.setenv("EMBEDDINGS_DIMENSIONS", "1024")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    with pytest.raises(SystemExit, match="empty api-key"):
        script.main()

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("EMBEDDINGS_MODEL", "mongodb/voyage-4-large")
    monkeypatch.setenv("DATABASE_URL", "postgres://unused")
    monkeypatch.setattr(script, "model_dimension", lambda key, model, extra: 1536)
    with pytest.raises(SystemExit, match="returns 1536 dimensions, not 1024"):
        script.main()
    monkeypatch.setattr(script, "model_dimension", lambda key, model, extra: 1024)

    async def rows_at_384(url):
        return {"memory_units": (V384, 3)}

    monkeypatch.setattr(script, "database", rows_at_384)
    with pytest.raises(SystemExit, match=r"holds 3 rows as vector\(384\)"):
        script.main()

    async def empty(url):
        return {"memory_units": (V384, 0)}

    monkeypatch.setattr(script, "database", empty)
    script.main()


def test_recovery_job_runs_what_rollback_test_proves():
    job = manifest("recovery/reembed-local.yaml", "Job")
    container, env = container_env(job["spec"]["template"]["spec"])
    deployment = manifest("resources/hindsight.yaml", "Deployment")
    api, api_env = container_env(deployment["spec"]["template"]["spec"])
    assert container["image"] == api["image"]
    assert container["command"] == [
        "/app/api/.venv/bin/hindsight-admin",
        "reembed",
        "--all-schemas",
    ]
    assert env["HINDSIGHT_API_DATABASE_URL"] == api_env["HINDSIGHT_API_DATABASE_URL"]
    # rollback_test's local_env: the image's model, no LLM, no Hugging Face egress.
    assert env["HINDSIGHT_API_EMBEDDINGS_PROVIDER"] == "local"
    assert env["HINDSIGHT_API_LLM_PROVIDER"] == "none"
    assert env["HF_HUB_OFFLINE"] == env["TRANSFORMERS_OFFLINE"] == "1"
    # Break-glass only: ArgoCD never applies it.
    for path in ("kustomization.yaml", "resources/kustomization.yaml"):
        (kustomization,) = read(path)
        assert not any("recovery" in r for r in kustomization.get("resources", []))
