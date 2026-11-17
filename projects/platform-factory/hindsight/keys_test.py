"""Exercise Hindsight's key Jobs against their Secret and RBAC manifests."""

import asyncio
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
    # From outside the namespace, only the egress gateway; inside it, only the
    # bank-template Job (HOME-183), both on the API port.
    policy = next(
        d
        for d in read("resources/network-policy.yaml")
        if d["metadata"]["name"] == "hindsight"
    )
    gateway, template = policy["spec"]["ingress"]
    assert gateway["fromEndpoints"] == [
        {
            "matchLabels": {
                "k8s:io.kubernetes.pod.namespace": "ate-system",
                "app": "atenet-egress",
            }
        }
    ]
    assert template["fromEndpoints"] == [
        {
            "matchLabels": {
                "k8s:io.kubernetes.pod.namespace": "hindsight",
                "app.kubernetes.io/name": "hindsight-bank-template",
            }
        }
    ]
    for rule in (gateway, template):
        assert rule["toPorts"] == [{"ports": [{"port": "8888", "protocol": "TCP"}]}]
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


def test_a_decision_model_ranks_recall_and_no_local_model_loads():
    # HOME-182: Jev 1.14 through OpenRouter's System One endpoint, on the
    # shared key; no reranker model and no local fallback (Joe on #8147,
    # comment 6949878652).
    (deployment,) = [
        d for d in read("resources/hindsight.yaml") if d["kind"] == "Deployment"
    ]
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e.get("value", e.get("valueFrom")) for e in container["env"]}
    assert env["HINDSIGHT_API_DECISION_MODEL_PROVIDER"] == "typesafe"
    assert env["HINDSIGHT_API_DECISION_MODEL_BASE_URL"] == "https://openrouter.ai/api"
    assert env["HINDSIGHT_API_DECISION_MODEL_MODEL"] == "typesafe/jev-1.14"
    assert env["HINDSIGHT_API_DECISION_MODEL_API_KEY"] == {
        "secretKeyRef": {"name": "hindsight-openrouter", "key": "api-key"}
    }
    assert env["HINDSIGHT_API_RERANKER_PROVIDER"] == "rrf"
    assert "HINDSIGHT_API_RECALL_DECISION_MODEL_FALLBACK" not in env
    # Embeddings are remote too, so nothing loads a model in the pod.
    assert env["HINDSIGHT_API_EMBEDDINGS_PROVIDER"] == "openrouter"


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
    assert generators == {
        "hindsight-bank-template",
        "hindsight-keys-script",
        "hindsight-preflight-script",
    }
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
        ("DECISION_MODEL", "HINDSIGHT_API_DECISION_MODEL_MODEL"),
        ("DECISION_MODEL_BASE_URL", "HINDSIGHT_API_DECISION_MODEL_BASE_URL"),
        ("OPENROUTER_API_KEY", "HINDSIGHT_API_DECISION_MODEL_API_KEY"),
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
    monkeypatch.setenv("DECISION_MODEL", "typesafe/jev-1.14")
    monkeypatch.setenv("DECISION_MODEL_BASE_URL", "https://openrouter.ai/api")
    monkeypatch.setenv("DATABASE_URL", "postgres://unused")
    monkeypatch.setattr(script, "decision_endpoints", lambda key, model: ["typesafe"])
    asked = []
    monkeypatch.setattr(script, "decision_answers", lambda *args: asked.append(args[3]))
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
    # Hindsight cannot send a data policy, so the one endpoint must answer a
    # request that requires zero data retention.
    assert asked[-1] == {
        "zdr": True,
        "data_collection": "deny",
        "allow_fallbacks": False,
    }

    # Recall has no local fallback, so a decision model that fails stops it.
    def refused(key, url, model, provider):
        raise SystemExit(f"The decision model {model} refused: HTTP 404")

    monkeypatch.setattr(script, "decision_answers", refused)
    with pytest.raises(SystemExit, match="decision model typesafe/jev-1.14 refused"):
        script.main()

    # A second endpoint could keep data, and no preference can pin one variant.
    monkeypatch.setattr(
        script, "decision_endpoints", lambda key, model: ["upstage", "upstage/zdr"]
    )
    with pytest.raises(SystemExit, match="not exactly one"):
        script.main()


def test_preflight_reads_every_schema(monkeypatch):
    # reembed --all-schemas covers tenant schemas; so must the preflight.
    script = load_preflight()

    class Conn:
        async def fetch(self, query, tables):
            assert tables == ["memory_units", "mental_models"]
            return [
                ("public", "memory_units", V384),
                ("public", "mental_models", V384),
                ("tenant_a", "memory_units", V384),
            ]

        async def fetchval(self, query):
            return 5 if '"tenant_a"."memory_units"' in query else 0

    columns = asyncio.run(script.stored(Conn()))
    assert columns == {
        "public.memory_units": (V384, 0),
        "public.mental_models": (V384, 0),
        "tenant_a.memory_units": (V384, 5),
    }
    assert script.refused(columns, 1024) == [
        "tenant_a.memory_units holds 5 rows as vector(384)"
    ]


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


def test_new_banks_start_with_schema_v1():
    # HOME-183: extraction tags each fact app:<project> and kind:<kind>, and
    # consolidation builds observations per project.
    template = json.loads((ROOT / "resources/bank-template.json").read_text())
    bank = template["bank"]
    assert template["version"] == "1"
    labels = {g["key"]: g for g in bank["entity_labels"]}
    assert set(labels) == {"app", "kind"}
    assert all(g["tag"] and g["optional"] for g in labels.values())
    # One project per fact, so the app:* scope gives each project its own
    # consolidation pass (boundary_test checks it with Hindsight's code).
    assert labels["app"]["type"] == "value"
    apps = [v["value"] for v in labels["app"]["values"]]
    assert apps == sorted(set(apps)), "app values must be unique and sorted"
    # A directory name under projects/, or under one of the three platform
    # groups, whose own roots are values too. No "..", no deeper paths.
    groups = ("platform", "platform-gke", "platform-factory")
    path = re.compile(r"(?:(?:" + "|".join(groups) + r")/)?[a-z0-9][a-z0-9-]*")
    assert [a for a in apps if not path.fullmatch(a)] == []
    assert set(groups) <= set(apps)
    assert {"factory", "platform-factory/hindsight"} <= set(apps)
    assert [v["value"] for v in labels["kind"]["values"]] == [
        "lesson",
        "incident",
        "decision",
        "reference",
    ]
    assert bank["entities_allow_free_form"] is True
    assert bank["default_observation_scopes"] == [["app:*"]]

    container, env = container_env(
        manifest("resources/hindsight.yaml", "Deployment")["spec"]["template"]["spec"]
    )
    assert env["HINDSIGHT_API_DEFAULT_BANK_TEMPLATE"] == {
        "configMapKeyRef": {
            "name": "hindsight-bank-template",
            "key": "bank-template.json",
        }
    }


def test_bank_homelab_gets_the_template_after_every_sync():
    # HOME-183: the default template applies only when Hindsight creates a
    # bank, so a PostSync Job also imports it into homelab.
    job = manifest("resources/hindsight.yaml", "Job", "hindsight-bank-template")
    annotations = job["metadata"]["annotations"]
    assert annotations["argocd.argoproj.io/hook"] == "PostSync"
    assert annotations["argocd.argoproj.io/hook-delete-policy"] == "BeforeHookCreation"
    pod = job["spec"]["template"]["spec"]
    container, env = container_env(pod)
    assert container["command"][-1] == "/template/apply_template.py"
    assert env["BANK"] == "homelab"
    assert env["TENANT_API_KEY"] == {
        "secretKeyRef": {"name": "hindsight-keys", "key": "tenant-api-key"}
    }
    assert pod["volumes"] == [
        {"name": "template", "configMap": {"name": "hindsight-bank-template"}}
    ]
    (kustomization,) = read("resources/kustomization.yaml")
    (generator,) = [
        g
        for g in kustomization["configMapGenerator"]
        if g["name"] == "hindsight-bank-template"
    ]
    assert generator["files"] == ["apply_template.py", "bank-template.json"]
    # Only this Job reaches the REST port from inside the namespace.
    policies = {d["metadata"]["name"]: d for d in read("resources/network-policy.yaml")}
    job_label = {"app.kubernetes.io/name": "hindsight-bank-template"}
    assert policies["hindsight-bank-template"]["spec"]["endpointSelector"] == {
        "matchLabels": job_label
    }
    (egress,) = policies["hindsight-bank-template"]["spec"]["egress"]
    assert egress["toEndpoints"] == [
        {
            "matchLabels": {
                "app.kubernetes.io/name": "hindsight",
                "app.kubernetes.io/component": "api",
            }
        }
    ]
    sources = [
        e["matchLabels"]
        for rule in policies["hindsight"]["spec"]["ingress"]
        for e in rule["fromEndpoints"]
    ]
    assert {"k8s:io.kubernetes.pod.namespace": "hindsight", **job_label} in sources


def load_apply():
    spec = importlib.util.spec_from_file_location(
        "hindsight_apply_template", ROOT / "resources/apply_template.py"
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    return script


class FakeHindsight:
    """Serve the four REST calls the Job makes, recording them."""

    def __init__(self, banks, config=None, applied=True):
        self.banks, self.config, self.applied, self.calls = banks, config, applied, []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        if path == "/health":
            return 200, {}
        if path.startswith("/v1/default/banks?"):
            return 200, {"banks": [{"bank_id": b} for b in self.banks]}
        if method == "PUT":
            self.banks.append(path.rsplit("/", 1)[1])
            return 200, {}
        if path.endswith("/import"):
            if self.config is None:
                self.config = body["bank"]
            return 200, {"config_applied": self.applied}
        return 200, {"config": self.config}


def returned(bank):
    # Hindsight fills defaults into what it stores (seen live, 2026-11-17).
    out = json.loads(json.dumps(bank))
    for group in out["entity_labels"]:
        group["fields"] = {}
        for value in group["values"]:
            value.setdefault("description", "")
    return out


def test_apply_creates_imports_and_verifies(capsys):
    script = load_apply()
    template = json.loads((ROOT / "resources/bank-template.json").read_text())
    fake = FakeHindsight([], config=returned(template["bank"]))
    script.apply(fake, "homelab", template, wait=0)
    assert ("PUT", "/v1/default/banks/homelab") in fake.calls
    assert ("POST", "/v1/default/banks/homelab/import") in fake.calls
    assert "carries the template" in capsys.readouterr().out

    # An existing bank is not created again; the import still runs.
    fake = FakeHindsight(["homelab"], config=returned(template["bank"]))
    script.apply(fake, "homelab", template, wait=0)
    assert ("PUT", "/v1/default/banks/homelab") not in fake.calls
    assert ("POST", "/v1/default/banks/homelab/import") in fake.calls


def test_apply_fails_when_the_bank_does_not_take_the_template():
    script = load_apply()
    template = json.loads((ROOT / "resources/bank-template.json").read_text())
    stale = returned(template["bank"])
    stale["entity_labels"] = stale["entity_labels"][1:]
    with pytest.raises(SystemExit, match="differs from the template"):
        script.apply(
            FakeHindsight(["homelab"], config=stale), "homelab", template, wait=0
        )
    # Descriptions steer extraction, so a changed one fails too.
    stale = returned(template["bank"])
    stale["entity_labels"][1]["values"][0]["description"] = "Something else."
    with pytest.raises(SystemExit, match="differs from the template"):
        script.apply(
            FakeHindsight(["homelab"], config=stale), "homelab", template, wait=0
        )
    with pytest.raises(SystemExit, match="import into homelab"):
        script.apply(
            FakeHindsight(["homelab"], applied=False), "homelab", template, wait=0
        )
