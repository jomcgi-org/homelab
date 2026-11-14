"""Pins for the factory chart's itsaplan: upstream images by digest, generated
secrets kept and never rendered, restricted pods, the edge route's path mode,
least-privilege Job roles and policies that select the pods they guard."""

from __future__ import annotations

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


def _one(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    found = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(found) == 1, f"expected one {kind}/{name}, found {len(found)}"
    return found[0]


def _pods(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        d["metadata"]["name"]: d["spec"]["template"]
        for d in _ours(docs)
        if d["kind"] in ("Deployment", "Job")
    }


def test_disabled_renders_nothing_in_factory() -> None:
    assert not _ours(_render(["itsaplan.enabled=false"]))


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
        "factory-itsaplan-bootstrap",
        "factory-itsaplan-agent-homelab-codex",
        "factory-itsaplan-agent-factory-coordinator",
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
        "factory-itsaplan-agent-homelab-codex",
        "factory-itsaplan-agent-factory-coordinator",
    ]
    assert bootstrap[1]["verbs"] == ["get", "update"]


def test_bootstrap_config_lists_the_agents() -> None:
    config = json.loads(
        _one(_render(), "ConfigMap", "factory-itsaplan-scripts")["data"]["config.json"]
    )
    assert [a["username"] for a in config["agents"]] == [
        "homelab-codex",
        "factory-coordinator",
    ]
    assert config["project"]["key"] == "HOME"
    assert config["oidc"]["clientId"] == "factory-itsaplan"


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
