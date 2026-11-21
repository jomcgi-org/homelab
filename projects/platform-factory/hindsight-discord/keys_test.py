"""Exercise Hindsight's key Jobs against their Secret and RBAC manifests."""

import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
from unittest.mock import Mock, mock_open, patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parent
KEYS = ("hindsight-discord", "hindsight-keys")
BRIDGE_KEY = ("factory-discord", "hindsight-discord-key")


def read(path):
    return [doc for doc in yaml.safe_load_all((ROOT / path).read_text()) if doc]


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    # Materialize runfiles: Kustomize's load restriction rejects symlinks that
    # resolve outside a resource directory in a Bazel sandbox.
    root = tmp_path_factory.mktemp("kustomize")
    documents = []
    for directory in ("resources", "discord"):
        target = root / directory
        shutil.copytree(ROOT / directory, target)
        result = subprocess.run(
            [os.environ.get("KUSTOMIZE_BIN", "kustomize"), "build", str(target)],
            check=True,
            capture_output=True,
            text=True,
        )
        documents.extend(doc for doc in yaml.safe_load_all(result.stdout) if doc)
    return documents


class SecretAPI:
    """Serve manifest placeholders, enforcing the Jobs' bound Roles."""

    def __init__(self, documents):
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
        # Fail immediately on an RBAC regression; the real initializer would
        # retry GET's 403 until its 15-minute deadline.
        assert (*key, verb) in self.permissions, f"RBAC denies {verb} {key}"
        if method == "GET":
            return copy.deepcopy(self.secrets.get(key))
        assert key in self.secrets, "ArgoCD must create the placeholder first"
        self.secrets[key] = copy.deepcopy(body)
        self.writes.append(key)
        return copy.deepcopy(body)


@pytest.fixture
def keys(monkeypatch, rendered):
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
    api = SecretAPI(rendered)
    monkeypatch.setattr(script, "call", api.call)
    values = iter(["a" * 64])
    monkeypatch.setattr(
        script.secrets, "token_hex", Mock(side_effect=lambda _: next(values))
    )
    return script, api


def test_rendered_key_job_permissions_and_mounts(rendered):
    role = next(
        doc
        for doc in rendered
        if doc["kind"] == "Role"
        and doc["metadata"]["namespace"] == KEYS[0]
        and doc["metadata"]["name"] == "hindsight-keys"
    )
    assert role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "resourceNames": [KEYS[1]],
            "verbs": ["get", "update"],
        }
    ]
    configmap = next(
        doc
        for doc in rendered
        if doc["kind"] == "ConfigMap"
        and doc["metadata"]["name"].startswith("hindsight-keys-script-")
    )
    assert configmap["data"]["keys.py"] == (ROOT / "resources/keys.py").read_text()
    jobs = [
        doc
        for doc in rendered
        if doc["kind"] == "Job" and doc["metadata"]["name"] != "hindsight-preflight"
    ]
    assert len(jobs) == 2
    for job in jobs:
        (volume,) = job["spec"]["template"]["spec"]["volumes"]
        assert volume == {
            "name": "scripts",
            "configMap": {"name": configmap["metadata"]["name"]},
        }


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
                        "app.kubernetes.io/name": "factory-discord-bridge",
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
    assert key_policy["spec"]["egress"] == [
        {
            "toEntities": ["kube-apiserver"],
            "toPorts": [{"ports": [{"port": "6443", "protocol": "TCP"}]}],
        }
    ]
    (client,) = read("discord/network-policy.yaml")
    assert client["metadata"]["namespace"] == "factory-discord"
    assert client["spec"] == {
        "endpointSelector": {
            "matchLabels": {"app.kubernetes.io/name": "factory-discord-bridge"}
        },
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


def api_env(doc):
    (container,) = doc["spec"]["template"]["spec"]["containers"]
    return {e["name"]: e.get("value", e.get("valueFrom")) for e in container["env"]}


def test_models_are_the_factory_hindsight_models_on_the_dedicated_key():
    # The factory Hindsight's embeddings (HOME-176) and decision model
    # (HOME-182), on the Discord key: every model call that sees Discord
    # content uses hindsight-openrouter (HOME-162 contract rule 2).
    deployment = next(
        d for d in read("resources/hindsight.yaml") if d["kind"] == "Deployment"
    )
    env = api_env(deployment)
    key = {"secretKeyRef": {"name": "hindsight-openrouter", "key": "api-key"}}
    assert env["HINDSIGHT_API_EMBEDDINGS_PROVIDER"] == "openrouter"
    assert env["HINDSIGHT_API_EMBEDDINGS_OPENROUTER_MODEL"] == "mongodb/voyage-4-large"
    assert env["HINDSIGHT_API_EMBEDDINGS_OPENROUTER_API_KEY"] == key
    assert json.loads(env["HINDSIGHT_API_EMBEDDINGS_OPENAI_EXTRA_BODY"]) == {
        "provider": {"data_collection": "deny"}
    }
    assert env["HINDSIGHT_API_DECISION_MODEL_PROVIDER"] == "typesafe"
    assert env["HINDSIGHT_API_DECISION_MODEL_BASE_URL"] == "https://openrouter.ai/api"
    assert env["HINDSIGHT_API_DECISION_MODEL_MODEL"] == "typesafe/jev-1.14"
    assert env["HINDSIGHT_API_DECISION_MODEL_API_KEY"] == key
    assert env["HINDSIGHT_API_RERANKER_PROVIDER"] == "rrf"
    assert "HINDSIGHT_API_RECALL_DECISION_MODEL_FALLBACK" not in env
    secret_refs = {
        (v["secretKeyRef"]["name"], v["secretKeyRef"]["key"])
        for v in env.values()
        if isinstance(v, dict) and "secretKeyRef" in v
    }
    assert secret_refs == {
        ("hindsight-openrouter", "api-key"),
        ("hindsight-openrouter", "base-url"),
        ("hindsight-keys", "tenant-api-key"),
        ("hindsight-db", "DATABASE_URL"),
    }


def test_preflight_is_the_factory_script_and_runs_before_the_deployment():
    factory = ROOT.parent / "hindsight/resources/preflight.py"
    assert (ROOT / "resources/preflight.py").read_bytes() == factory.read_bytes()
    docs = read("resources/hindsight.yaml")
    job = next(
        d
        for d in docs
        if d["kind"] == "Job" and d["metadata"]["name"] == "hindsight-preflight"
    )
    annotations = job["metadata"]["annotations"]
    assert annotations["argocd.argoproj.io/hook"] == "Sync"
    assert annotations["argocd.argoproj.io/hook-delete-policy"] == "BeforeHookCreation"
    assert annotations["argocd.argoproj.io/sync-wave"] == "-1"
    for doc in read("resources/secrets.yaml"):
        if doc["kind"] in ("OnePasswordItem", "Secret"):
            wave = doc["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]
            assert wave == "-2"
    account = next(d for d in docs if d["kind"] == "ServiceAccount")
    assert account["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "-2"
    pod = job["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "hindsight"
    assert pod["automountServiceAccountToken"] is False
    assert pod["volumes"] == [
        {"name": "scripts", "configMap": {"name": "hindsight-preflight-script"}}
    ]
    deployment = next(d for d in docs if d["kind"] == "Deployment")
    (api,) = deployment["spec"]["template"]["spec"]["containers"]
    (check,) = pod["containers"]
    assert check["image"] == api["image"]
    env, api = api_env(job), api_env(deployment)
    assert env["EMBEDDINGS_MODEL"] == api["HINDSIGHT_API_EMBEDDINGS_OPENROUTER_MODEL"]
    assert (
        env["EMBEDDINGS_EXTRA_BODY"]
        == api["HINDSIGHT_API_EMBEDDINGS_OPENAI_EXTRA_BODY"]
    )
    assert env["EMBEDDINGS_DIMENSIONS"] == "1024"
    assert env["DECISION_MODEL"] == api["HINDSIGHT_API_DECISION_MODEL_MODEL"]
    assert (
        env["DECISION_MODEL_BASE_URL"] == api["HINDSIGHT_API_DECISION_MODEL_BASE_URL"]
    )
    assert (
        env["OPENROUTER_API_KEY"] == api["HINDSIGHT_API_EMBEDDINGS_OPENROUTER_API_KEY"]
    )
    assert env["DATABASE_URL"] == api["HINDSIGHT_API_DATABASE_URL"]


def test_service_selects_only_the_api_pod():
    docs = read("resources/hindsight.yaml")
    service = next(d for d in docs if d["kind"] == "Service")
    deployment = next(d for d in docs if d["kind"] == "Deployment")
    job = next(d for d in docs if d["kind"] == "Job")
    selector = service["spec"]["selector"]
    pod_labels = deployment["spec"]["template"]["metadata"]["labels"]
    assert selector.items() <= pod_labels.items()
    assert not selector.items() <= job["spec"]["template"]["metadata"]["labels"].items()
    # The network policy selects the name label, so it covers both pods.
    policy = read("resources/network-policy.yaml")[0]
    for labels in (pod_labels, job["spec"]["template"]["metadata"]["labels"]):
        assert (
            policy["spec"]["endpointSelector"]["matchLabels"].items() <= labels.items()
        )


def test_recovery_job_reembeds_discord_memory_with_the_tested_settings():
    # The factory Hindsight's rollback_test proves this command and these
    # settings in the image; this Job targets the Discord database.
    (job,) = read("recovery/reembed-local.yaml")
    assert job["kind"] == "Job"
    assert job["metadata"]["namespace"] == "hindsight-discord"
    deployment = next(
        d for d in read("resources/hindsight.yaml") if d["kind"] == "Deployment"
    )
    (container,) = job["spec"]["template"]["spec"]["containers"]
    (api,) = deployment["spec"]["template"]["spec"]["containers"]
    assert container["image"] == api["image"]
    assert container["command"] == [
        "/app/api/.venv/bin/hindsight-admin",
        "reembed",
        "--all-schemas",
    ]
    env, deployed = api_env(job), api_env(deployment)
    assert env["HINDSIGHT_API_DATABASE_URL"] == deployed["HINDSIGHT_API_DATABASE_URL"]
    assert env["HINDSIGHT_API_EMBEDDINGS_PROVIDER"] == "local"
    assert env["HINDSIGHT_API_LLM_PROVIDER"] == "none"
    assert env["HF_HUB_OFFLINE"] == env["TRANSFORMERS_OFFLINE"] == "1"
    labels = job["spec"]["template"]["metadata"]["labels"]
    selector = read("resources/network-policy.yaml")[0]["spec"]["endpointSelector"]
    assert selector["matchLabels"].items() <= labels.items()
    # Break-glass only: ArgoCD never applies it.
    for path in ("kustomization.yaml", "resources/kustomization.yaml"):
        (kustomization,) = read(path)
        assert not any("recovery" in r for r in kustomization.get("resources", []))
