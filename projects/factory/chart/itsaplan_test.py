"""Pins for the factory chart's itsaplan: upstream images by digest, generated
secrets kept and never rendered, restricted pods, the edge route's path mode,
least-privilege Job roles and policies that select the pods they guard."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parent
_DEPLOY_VALUES = Path(
    os.environ.get("DEPLOY_VALUES", _CHART_DIR.parent / "deploy" / "values.yaml")
)
_APPLICATION = Path(
    os.environ.get("APPLICATION", _CHART_DIR.parent / "deploy" / "application.yaml")
)
_NS = "factory"
# The factory's deploy values, with itsaplan enabled as the follow-up
# enablement will set it.
_ENABLE = [
    "--set",
    "itsaplan.enabled=true",
    "--set-json",
    'itsaplan.database.endpoints=[{"matchLabels":{"k8s:io.kubernetes.pod.namespace":'
    '"factory-db","cnpg.io/cluster":"factory-db"}}]',
    "--set-json",
    'itsaplan.s3.endpoints=[{"matchLabels":{"k8s:io.kubernetes.pod.namespace":'
    '"garage","app.kubernetes.io/name":"garage"}}]',
]
_KEEP = {
    "argocd.argoproj.io/sync-options": "Prune=false,Delete=false",
    "helm.sh/resource-policy": "keep",
}


def _render(overrides: list[str] | None = None) -> list[dict[str, Any]]:
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "factory",
        str(_CHART_DIR),
        "--namespace",
        "kagent",
        "--values",
        str(_DEPLOY_VALUES),
        *_ENABLE,
    ]
    for override in overrides or []:
        argv += ["--set", override]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def _ours(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """itsaplan's objects; the broker shares the namespace."""
    return [
        d
        for d in docs
        if d["metadata"].get("namespace") == _NS
        and d["metadata"]["name"].startswith("factory-itsaplan")
    ]


def _one(
    docs: list[dict[str, Any]], kind: str, name: str, namespace: str | None = None
) -> dict[str, Any]:
    found = [
        d
        for d in docs
        if d["kind"] == kind
        and d["metadata"]["name"] == name
        and (namespace is None or d["metadata"].get("namespace") == namespace)
    ]
    assert len(found) == 1, f"expected one {kind}/{name}, found {len(found)}"
    return found[0]


def _pods(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        d["metadata"]["name"]: d["spec"]["template"]
        for d in _ours(docs)
        if d["kind"] in ("Deployment", "Job")
    }


def test_disabled_renders_nothing_in_factory() -> None:
    assert not _ours(_render(["itsaplan.enabled=false", "connector.enabled=false"]))


def test_no_namespace_of_its_own() -> None:
    # The broker's template declares the factory namespace; itsaplan adds none.
    namespaces = [
        d
        for d in _render()
        if d["kind"] == "Namespace" and d["metadata"]["name"] == _NS
    ]
    assert len(namespaces) <= 1


def test_images_are_upstream_by_digest() -> None:
    for name, pod in _pods(_render()).items():
        spec = pod["spec"]
        for c in spec.get("initContainers", []) + spec["containers"]:
            assert "@sha256:" in c["image"], f"{name}/{c['name']} is not pinned"
    docs = _render()
    for component in ("api", "web", "worker"):
        image = _one(docs, "Deployment", f"factory-itsaplan-{component}")["spec"][
            "template"
        ]["spec"]["containers"][0]["image"]
        assert image.startswith(f"ghcr.io/croffasia/itsaplan-{component}:1.10.0@")


def test_generated_secrets_are_kept_and_never_rendered() -> None:
    # The chart declares each Secret empty; Jobs and other namespaces fill it,
    # and nothing about it may change across upgrades (ARCHITECTURE.md).
    secrets = [d for d in _ours(_render()) if d["kind"] == "Secret"]
    assert {s["metadata"]["name"] for s in secrets} == {
        "factory-itsaplan",
        "factory-itsaplan-admin",
        "factory-itsaplan-joe-sessions",
        "factory-itsaplan-bootstrap",
        "factory-itsaplan-agent-homelab-codex",
        "factory-itsaplan-agent-factory-coordinator",
        "factory-itsaplan-agent-factory-reviewer",
    }
    for s in secrets:
        assert "data" not in s and "stringData" not in s
        assert _KEEP.items() <= s["metadata"]["annotations"].items()


def test_pods_are_restricted() -> None:
    for name, pod in _pods(_render()).items():
        spec = pod["spec"]
        assert spec["securityContext"]["runAsNonRoot"] is True, name
        for c in spec.get("initContainers", []) + spec["containers"]:
            sc = c["securityContext"]
            assert sc["readOnlyRootFilesystem"] is True, f"{name}/{c['name']}"
            assert sc["allowPrivilegeEscalation"] is False
            assert sc["capabilities"]["drop"] == ["ALL"]
        if not name.endswith(("-secret-init", "-bootstrap")):
            # The workloads never call the Kubernetes API.
            assert spec["automountServiceAccountToken"] is False, name


def test_api_runs_migrations_first_and_web_holds_no_secret() -> None:
    docs = _render()
    api = _one(docs, "Deployment", "factory-itsaplan-api")["spec"]["template"]
    assert api["spec"]["initContainers"][0]["command"][-1] == (
        "packages/db/src/migrate.ts"
    )
    env = {e["name"]: e for e in api["spec"]["containers"][0]["env"]}
    assert env["DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": "factory-itsaplan-db",
        "key": "DATABASE_URL",
    }
    assert env["S3_SECRET_ACCESS_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "factory-itsaplan-s3",
        "key": "AWS_SECRET_ACCESS_KEY",
    }
    web = _one(docs, "Deployment", "factory-itsaplan-web")["spec"]["template"]
    assert "env" not in web["spec"]["containers"][0]
    config = _one(docs, "ConfigMap", "factory-itsaplan")["data"]
    assert config["API_URL"] == "https://factory-plan.jomcgi.dev/backend"
    assert config["APP_URL"] == "https://factory-plan.jomcgi.dev"
    assert config["SERVICE_URL_API"] == "http://factory-itsaplan-api.factory.svc:3000"


def test_route_strips_the_api_prefix() -> None:
    route = _one(_render(), "HTTPRoute", "factory-itsaplan")
    assert route["metadata"]["namespace"] == _NS
    assert route["spec"]["parentRefs"][0]["name"] == "edge"
    assert route["spec"]["parentRefs"][0]["namespace"] == "edge"
    assert route["spec"]["hostnames"] == ["factory-plan.jomcgi.dev"]
    blocked, api, web = route["spec"]["rules"]
    assert [m["path"]["value"] for m in blocked["matches"]] == [
        "/backend/mcp",
        "/backend/agent-runs/claim",
        "/backend/agent-chats/claim",
        "^/backend/agent-(runs|chats)/[^/]+/(heartbeat|events|result)/?$",
    ]
    assert "backendRefs" not in blocked
    assert blocked["filters"][0]["extensionRef"]["name"] == (
        "factory-itsaplan-not-public"
    )
    not_public = _one(_render(), "HTTPRouteFilter", "factory-itsaplan-not-public")
    assert not_public["spec"]["directResponse"]["statusCode"] == 404
    assert api["matches"][0]["path"] == {"type": "PathPrefix", "value": "/backend"}
    assert api["filters"][0]["urlRewrite"]["path"] == {
        "type": "ReplacePrefixMatch",
        "replacePrefixMatch": "/",
    }
    assert api["backendRefs"][0]["name"] == "factory-itsaplan-api"
    assert web["matches"][0]["path"] == {"type": "PathPrefix", "value": "/"}
    assert web["backendRefs"][0]["name"] == "factory-itsaplan-web"


def test_job_roles_name_only_their_secrets() -> None:
    docs = _render()
    secret_init = _one(docs, "Role", "factory-itsaplan-secret-init")["rules"]
    assert secret_init == [
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "resourceNames": ["factory-itsaplan", "factory-itsaplan-admin"],
            "verbs": ["get", "update"],
        },
        {
            "apiGroups": ["apps"],
            "resources": ["deployments"],
            "resourceNames": ["factory-itsaplan-api", "factory-itsaplan-worker"],
            "verbs": ["patch"],
        },
    ]
    bootstrap = _one(docs, "Role", "factory-itsaplan-bootstrap")["rules"]
    assert bootstrap[0]["resourceNames"] == [
        "factory-itsaplan-admin",
        "factory-itsaplan-oidc",
    ]
    assert bootstrap[0]["verbs"] == ["get"]
    assert bootstrap[1]["resourceNames"] == [
        "factory-itsaplan-bootstrap",
        "factory-itsaplan-joe-sessions",
        "factory-itsaplan-agent-homelab-codex",
        "factory-itsaplan-agent-factory-coordinator",
        "factory-itsaplan-agent-factory-reviewer",
    ]
    assert bootstrap[1]["verbs"] == ["get", "update"]


def test_sessions_secret_is_accessible_only_to_bootstrap() -> None:
    docs = _render()
    name = "factory-itsaplan-joe-sessions"
    secret = _one(docs, "Secret", name)
    assert secret["metadata"]["namespace"] == _NS
    assert "substrate.ate.dev/injectable" not in secret["metadata"]["labels"]
    # Resolve all chart RBAC that could read this Secret, including wildcard
    # grants and ClusterRole bindings. No new reader or RoleBinding is needed.
    readers = []
    for binding in docs:
        kind = binding["kind"]
        if kind not in ("RoleBinding", "ClusterRoleBinding"):
            continue
        if kind == "RoleBinding" and binding["metadata"].get("namespace") != _NS:
            continue
        ref = binding["roleRef"]
        # Role names repeat across binding namespaces (factory-broker has
        # three). A RoleBinding resolves a Role only in its own namespace.
        role = _one(
            docs,
            ref["kind"],
            ref["name"],
            binding["metadata"]["namespace"] if ref["kind"] == "Role" else None,
        )
        for rule in role.get("rules", []):
            if (
                set(rule.get("apiGroups", [])) & {"", "*"}
                and set(rule.get("resources", [])) & {"secrets", "*"}
                and set(rule.get("verbs", [])) & {"get", "list", "watch", "*"}
                and (not rule.get("resourceNames") or name in rule["resourceNames"])
            ):
                readers.append(binding["metadata"]["name"])
                assert binding["subjects"] == [
                    {
                        "kind": "ServiceAccount",
                        "name": "factory-itsaplan-bootstrap",
                        "namespace": _NS,
                    }
                ]
    assert readers == ["factory-itsaplan-bootstrap"]
    assert {
        d["metadata"]["name"] for d in _ours(docs) if d["kind"] == "RoleBinding"
    } == {"factory-itsaplan-bootstrap", "factory-itsaplan-secret-init"}
    bootstrap = _one(docs, "Job", "factory-itsaplan-bootstrap")
    env = bootstrap["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {e["name"]: e.get("value") for e in env}["SESSIONS_SECRET"] == name
    # No pod, SandboxTemplate, credential mapping or injected environment
    # references the Secret. Bootstrap reads it via its Kubernetes Role.
    for doc in docs:
        if doc["kind"] in ("Secret", "Role", "ConfigMap") or doc is bootstrap:
            continue
        assert name not in json.dumps(doc), doc["metadata"]["name"]


def test_bootstrap_config_lists_the_agents() -> None:
    config = json.loads(
        _one(_render(), "ConfigMap", "factory-itsaplan-scripts")["data"]["config.json"]
    )
    assert [a["username"] for a in config["agents"]] == [
        "homelab-codex",
        "factory-coordinator",
        "factory-reviewer",
    ]
    assert config["project"]["key"] == "HOME"
    assert config["oidc"]["clientId"] == "factory-itsaplan"


@pytest.fixture
def bootstrap(tmp_path, monkeypatch):
    (tmp_path / "token").write_text("test-token")
    (tmp_path / "ca.crt").write_text("")
    config = {
        "project": {"key": "HOME"},
        "agents": [{"username": "factory-reviewer", "name": "Factory reviewer"}],
    }
    (tmp_path / "config.json").write_text(json.dumps(config))
    for key, value in {
        "SA_DIR": str(tmp_path),
        "KUBERNETES_SERVICE_HOST": "kubernetes",
        "KUBERNETES_SERVICE_PORT": "443",
        "NAMESPACE": "factory",
        "API_INTERNAL_URL": "http://api:3000",
        "APP_URL": "https://example.invalid",
        "CONFIG": str(tmp_path / "config.json"),
        "AGENT_SECRET_PREFIX": "factory-itsaplan-agent-",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("ssl.create_default_context", lambda **_: None)
    spec = importlib.util.spec_from_file_location(
        "bootstrap", _CHART_DIR / "files" / "itsaplan" / "bootstrap.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "k8s", lambda *_: {"data": {}})
    monkeypatch.setattr(module, "secret_data", lambda _: {"apiKey": "test-key"})
    monkeypatch.setattr(module, "key_works", lambda *_: True)
    return module


class ReviewerBootstrapAPI:
    def __init__(self):
        self.agent = None
        self.role = None
        self.member = None
        self.writes = []
        self.fail_role = False

    def call(self, method, path, body=None):
        if method != "GET":
            self.writes.append((method, path, copy.deepcopy(body)))
        if path == "/permission-catalog":
            return {
                "resources": [{"key": k} for k in ("work_items", "ai_agents", "states")],
                "actions": ["create", "read", "edit", "delete"],
            }
        if path.endswith("/roles/options"):
            return [self.role] if self.role else []
        if path.endswith("/roles") or path.endswith("/roles/7"):
            if self.fail_role:
                raise RuntimeError("role read/write failed")
            self.role = {"id": 7, "system": False, **(self.role or {}), **body}
            return self.role
        if path == "/teams/1/ai-agents":
            if method == "GET":
                return copy.deepcopy([self.agent] if self.agent else [])
            assert body["projectIds"] == []
            self.agent = {"id": 9, "userId": "reviewer-id", "projects": [], **body}
            return {"agent": copy.deepcopy(self.agent), "apiKey": "test-key"}
        if path == "/teams/1/ai-agents/9":
            self.agent.update(body)
            return copy.deepcopy(self.agent)
        if path.endswith("/projects"):
            self.agent["projects"] = [{"id": p} for p in body["projectIds"]]
            return copy.deepcopy(self.agent)
        if path.startswith("/projects/HOME/members?"):
            assert "pageSize=100" in path
            return {"items": [self.member]}
        if path.startswith("/projects/HOME/members"):
            self.member = {"userId": "reviewer-id", **body}
            self.agent["projects"] = [{"id": 3}]
            return None
        raise AssertionError((method, path))


def test_reviewer_bootstrap_is_read_only_before_key_publication(bootstrap, monkeypatch):
    api = ReviewerBootstrapAPI()
    published = []

    def publish(name, data):
        assert api.agent["projects"] == [{"id": 3}]
        assert api.member["role"] == "member" and api.member["roleId"] == 7
        granted = {
            (resource, action)
            for resource, actions in api.role["permissions"].items()
            for action, allowed in actions.items()
            if allowed
        }
        assert granted == {("work_items", "read")}
        assert not api.agent["triggerOnAssign"]
        assert not api.agent["triggerOnMention"]
        assert not api.agent["chatEnabled"]
        assert api.agent["fieldTriggers"] == []
        assert name == "factory-itsaplan-agent-factory-reviewer"
        published.append(data)

    monkeypatch.setattr(bootstrap, "write_secret", publish)
    bootstrap.ensure_agents(api, 1, 3)
    writes = len(api.writes)
    bootstrap.ensure_agents(api, 1, 3)
    assert len(api.writes) == writes  # no duplicate role, agent or membership
    assert published[0] == published[1]
    api.role["permissions"]["work_items"]["edit"] = True
    api.member["roleId"] = 99
    api.agent["projects"].append({"id": 4})
    api.agent["triggerOnMention"] = True
    bootstrap.ensure_agents(api, 1, 3)
    assert len(published) == 3  # publish() checks every repaired boundary


def test_reviewer_bootstrap_does_not_publish_when_role_fails(bootstrap, monkeypatch):
    api = ReviewerBootstrapAPI()
    api.fail_role = True
    published = []
    monkeypatch.setattr(bootstrap, "write_secret", lambda *args: published.append(args))
    with pytest.raises(RuntimeError, match="role read/write failed"):
        bootstrap.ensure_agents(api, 1, 3)
    assert not published


def test_policies_select_their_pods() -> None:
    docs = _render()
    pods = _pods(docs)
    for policy, workload in (
        ("factory-itsaplan-api", "factory-itsaplan-api"),
        ("factory-itsaplan-web", "factory-itsaplan-web"),
        ("factory-itsaplan-worker", "factory-itsaplan-worker"),
        ("factory-itsaplan-secret-init", "factory-itsaplan-secret-init"),
        ("factory-itsaplan-bootstrap", "factory-itsaplan-bootstrap"),
    ):
        selector = _one(docs, "CiliumNetworkPolicy", policy)["spec"][
            "endpointSelector"
        ]["matchLabels"]
        assert selector.items() <= pods[workload]["metadata"]["labels"].items()


def test_api_egress_is_database_s3_and_issuer() -> None:
    spec = _one(_render(), "CiliumNetworkPolicy", "factory-itsaplan-api")["spec"]
    ports = [r["toPorts"][0]["ports"][0]["port"] for r in spec["egress"]]
    assert ports == ["5432", "3900", "443"]
    assert spec["egress"][2]["toFQDNs"] == [{"matchName": "factory-auth.jomcgi.dev"}]
    assert spec["egress"][0]["toEndpoints"] == [
        {
            "matchLabels": {
                "k8s:io.kubernetes.pod.namespace": "factory-db",
                "cnpg.io/cluster": "factory-db",
            }
        }
    ]


def test_kagent_namespace_is_refused() -> None:
    with pytest.raises(RuntimeError, match="must not be kagent's"):
        _render(["itsaplan.namespace=kagent"])


def test_application_ignores_every_generated_secret() -> None:
    # The Jobs write these Secrets' data. Without an ignoreDifferences entry
    # self-heal would reset each to the chart's empty Secret. Checked only
    # once the deploy values enable itsaplan.
    deploy = yaml.safe_load(_DEPLOY_VALUES.read_text()) or {}
    if not (deploy.get("itsaplan") or {}).get("enabled"):
        pytest.skip("itsaplan is not enabled in the deploy values yet")
    declared = {
        d["metadata"]["name"] for d in _ours(_render()) if d["kind"] == "Secret"
    }
    app = yaml.safe_load(_APPLICATION.read_text())
    ignored = {
        e["name"]
        for e in app["spec"].get("ignoreDifferences", [])
        if e.get("kind") == "Secret"
        and e.get("namespace") == _NS
        and "/data" in e.get("jsonPointers", [])
    }
    assert declared <= ignored, f"not ignored: {sorted(declared - ignored)}"


def test_secret_init_counts_plans_and_purges_before_it_writes() -> None:
    docs = _render()
    job = _one(docs, "Job", "factory-itsaplan-secret-init")
    assert job["spec"]["activeDeadlineSeconds"] >= 1800
    pod = job["spec"]["template"]["spec"]
    steps = [c["name"] for c in pod["initContainers"]] + [
        c["name"] for c in pod["containers"]
    ]
    assert steps == ["db-state", "plan", "purge", "apply"]
    db_state, plan, purge = pod["initContainers"]
    assert db_state["image"].startswith("ghcr.io/croffasia/itsaplan-api:")
    assert "/scripts/db_state.sql > /state/rows" in db_state["command"][-1]
    assert plan["command"][-1] == "plan"
    assert '"$(cat /state/plan)" = purge' in purge["command"][-1]
    assert pod["containers"][0]["command"][-1] == "apply"
    scripts = _one(docs, "ConfigMap", "factory-itsaplan-scripts")["data"]
    assert "app_secret" in scripts["db_state.sql"]
    assert "delete from app_secret;" in scripts["purge.sql"]
    env = {e["name"]: e.get("value") for e in pod["containers"][0]["env"]}
    assert env["ROLL"] == "factory-itsaplan-api factory-itsaplan-worker"
