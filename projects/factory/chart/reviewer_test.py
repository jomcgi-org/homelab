"""Reviewer resources, credential separation and both ends of every network path."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART = Path(__file__).resolve().parent
_DEPLOY = Path(os.environ.get("DEPLOY_VALUES", _CHART.parent / "deploy/values.yaml"))
_LOCK = Path(os.environ.get("RENDER_LOCK", _CHART.parent / "deploy/render.sha256"))
_REVIEW_NS = "factory-r-homelab-review"
_SUBJECT = "system:serviceaccount:factory:factory-reviewer"
_KEEP = {
    "argocd.argoproj.io/sync-options": "Prune=false,Delete=false",
    "helm.sh/resource-policy": "keep",
}
_TWO_REPOSITORIES = [
    "connector.enabled=false",
    "repositories[0].name=homelab",
    "repositories[0].owner=jomcgi-org",
    "repositories[0].repo=homelab",
    "repositories[0].baseBranch=main",
    "repositories[0].namespace=factory-r-homelab",
    "repositories[0].agent=codex",
    "repositories[1].name=second",
    "repositories[1].owner=example",
    "repositories[1].repo=second",
    "repositories[1].baseBranch=trunk",
    "repositories[1].namespace=factory-r-second",
    "repositories[1].agent=codex",
    "kagent.rbac.namespaces={kagent,factory-r-homelab,factory-r-homelab-review,factory-r-second,factory-r-second-review}",
]


def _helm(
    overrides: list[str] | None = None,
    *,
    enable: bool = True,
    chart: Path = _CHART,
) -> str:
    args = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "factory",
        str(chart),
        "-n",
        "kagent",
        "-f",
        str(_DEPLOY),
    ]
    if enable:
        args += [
            "--set",
            "reviewer.enabled=true",
            "--set",
            "kagent.rbac.namespaces={kagent,factory-r-homelab,factory-r-homelab-review}",
        ]
    for override in overrides or []:
        args += ["--set-json" if override.endswith("=[]") else "--set", override]
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout


def _render(overrides: list[str] | None = None) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(_helm(overrides)) if doc]


def _in(
    docs: list[dict[str, Any]], kind: str, ns: str | None, name: str
) -> dict[str, Any]:
    found = [
        d
        for d in docs
        if d["kind"] == kind
        and d["metadata"]["name"] == name
        and d["metadata"].get("namespace") == ns
    ]
    assert len(found) == 1, (kind, ns, name, len(found))
    return found[0]


def _config(docs: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return yaml.safe_load(_in(docs, "ConfigMap", "factory", name)["data"]["config.yaml"])


def _pod(docs: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return _in(docs, "Deployment", "factory", name)["spec"]["template"]["spec"]


def test_default_off_is_absent_and_the_deploy_render_is_unchanged() -> None:
    assert yaml.safe_load((_CHART / "values.yaml").read_text())["reviewer"]["enabled"] is False
    rendered = _helm(enable=False)
    docs = [d for d in yaml.safe_load_all(rendered) if d]
    assert not any("review" in d["metadata"]["name"] for d in docs if d["kind"] == "Namespace")
    assert not any(d["metadata"]["name"] == "factory-reviewer" for d in docs)
    broker = _config(docs, "factory-broker")
    assert "githubReview" not in broker
    assert "reviewBindings" not in broker
    assert "reviewerSubjects" not in broker["tokenReview"]
    with tempfile.TemporaryDirectory() as tmp:
        bare = Path(tmp) / "chart"
        shutil.copytree(_CHART, bare)
        for template in (bare / "templates").glob("reviewer*.yaml"):
            template.unlink()
        assert rendered == _helm(enable=False, chart=bare)
    normalized = re.sub(r"(helm\.sh/chart: factory-)\S+", r"\g<1>VERSION", rendered)
    assert hashlib.sha256(normalized.encode()).hexdigest() == _LOCK.read_text().strip()


def test_one_kept_identity_and_private_image() -> None:
    docs = _render(["reviewer.image.digest=sha256:" + "c" * 64])
    deploy = _in(docs, "Deployment", "factory", "factory-reviewer")
    assert deploy["spec"]["replicas"] == 1
    assert deploy["spec"]["strategy"] == {"type": "Recreate"}
    pod = _pod(docs, "factory-reviewer")
    assert pod["serviceAccountName"] == "factory-reviewer"
    assert pod["automountServiceAccountToken"] is False
    assert pod["imagePullSecrets"] == [{"name": "ghcr-imagepull-secret"}]
    _in(docs, "OnePasswordItem", "factory", "ghcr-imagepull-secret")
    (container,) = pod["containers"]
    assert container["image"] == "ghcr.io/jomcgi/homelab/projects/factory/reviewer@sha256:" + "c" * 64
    assert container["ports"] == [{"name": "metrics", "containerPort": 9090}]
    assert container["env"] == [
        {"name": "FACTORY_REVIEWER_CONFIG", "value": "/etc/factory-reviewer/config.yaml"}
    ]
    for probe in ("readinessProbe", "livenessProbe"):
        assert container[probe]["httpGet"] == {"path": "/healthz", "port": "metrics"}
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    sa = _in(docs, "ServiceAccount", "factory", "factory-reviewer")
    assert _KEEP.items() <= sa["metadata"]["annotations"].items()
    assert sa["automountServiceAccountToken"] is False
    assert not any(
        s.get("name") == "factory-reviewer"
        for d in docs
        if d["kind"] in ("RoleBinding", "ClusterRoleBinding")
        for s in d.get("subjects", [])
    )


def test_tokens_and_config_paths_match_the_pod() -> None:
    docs = _render()
    pod = _pod(docs, "factory-reviewer")
    volumes = {v["name"]: v for v in pod["volumes"]}
    mounts = {m["name"]: m for m in pod["containers"][0]["volumeMounts"]}
    config = _config(docs, "factory-reviewer")
    for name, audience, key in (
        ("broker-token", "factory-broker", "brokerTokenFile"),
        ("kagent-token", "kagent-invocation", "kagentTokenFile"),
    ):
        assert volumes[name]["projected"]["sources"] == [
            {"serviceAccountToken": {"audience": audience, "expirationSeconds": 3600, "path": "token"}}
        ]
        assert config[key] == mounts[name]["mountPath"] + "/token"
        assert mounts[name]["readOnly"] is True
    assert volumes["config"]["configMap"]["name"] == "factory-reviewer"
    assert mounts["config"]["mountPath"] == "/etc/factory-reviewer"
    assert not any("secret" in v for v in pod["volumes"])
    assert config["brokerURL"] == "http://factory-broker.factory.svc:8081"
    assert config["kagentURL"] == "http://kagent-controller.kagent.svc:8083"
    assert config["metricsAddr"] == ":9090"
    assert config["appId"] == 5758326
    assert config["checkName"] == "factory review"
    assert config["concurrency"] == 2
    assert config["bindings"] == [{
        "name": "homelab", "owner": "jomcgi-org", "repo": "homelab", "baseBranch": "main",
        "kagentNamespace": _REVIEW_NS, "kagentAgent": "homelab-reviewer",
        "enqueueAuthors": ["jomcgi"], "humanApprovers": ["jomcgi"],
    }]
    checksum = _in(docs, "Deployment", "factory", "factory-reviewer")["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
    changed = _render(["reviewer.intervals.poll=10s"])
    assert checksum != _in(changed, "Deployment", "factory", "factory-reviewer")["spec"]["template"]["metadata"]["annotations"]["checksum/config"]


def test_review_binding_gets_isolated_agent_and_egress_credentials() -> None:
    docs = _render()
    ns = _in(docs, "Namespace", None, _REVIEW_NS)
    assert _KEEP.items() <= ns["metadata"]["annotations"].items()
    assert ns["metadata"]["labels"]["factory.jomcgi.dev/substrate-workers"] == "true"
    agent = _in(docs, "Agent", _REVIEW_NS, "homelab-reviewer")["spec"]
    inline = agent["template"]["inline"]
    assert inline["modelConfig"] == {"name": "reviewer"}
    assert inline["systemPrompt"].strip() == "you review pull requests read-only per the rules in your task; you never push, commit or open PRs"
    assert agent["harnessRef"] == {"name": "codex"}
    assert agent["egressPolicyRefs"] == [{"name": "github-git"}]
    model = _in(docs, "ModelConfig", _REVIEW_NS, "reviewer")["spec"]
    assert model == {
        "provider": "OpenAI", "model": "gpt-6-astra",
        "openAI": {"apiFormat": "responses", "baseUrl": "http://factory-broker.factory.svc.cluster.local/v1"},
        "actorJwt": {"audiences": ["factory-broker"]},
    }
    harness = _in(docs, "Harness", _REVIEW_NS, "codex")["spec"]
    assert harness["codex"] == {}
    assert harness["substrate"] == {
        "workerPoolRef": {"name": "homelab-reviewer"},
        "snapshotPolicy": {"location": "s3://ate-snapshots/factory-r-homelab-review/"},
    }
    assert {e["name"]: e["value"] for e in harness["env"]}["GIT_CONFIG_VALUE_0"] == "Authorization: Basic factory-placeholder"
    worker = _in(docs, "WorkerPool", _REVIEW_NS, "homelab-reviewer")["spec"]
    assert worker["replicas"] == 2
    assert worker["sandboxClasses"] == [{"name": "gvisor", "configRef": {"name": "gvisor-default"}}]
    assert worker["template"]["serviceAccountName"] == "homelab-reviewer-worker"
    assert worker["template"]["resources"]["limits"]["memory"] == "9Gi"
    sa = _in(docs, "ServiceAccount", _REVIEW_NS, "homelab-reviewer-worker")
    assert sa["automountServiceAccountToken"] is False
    assert _in(docs, "EgressPolicy", _REVIEW_NS, "github-git")["spec"] == {
        "destinations": [{
            "origin": "https://github.com",
            "authentication": {"header": "Authorization", "prefix": "Basic ", "secretKeyRef": {"name": "github-git", "key": "basic"}},
        }]
    }
    rb = _in(docs, "RoleBinding", _REVIEW_NS, "substrate-credential-provider")
    assert rb["roleRef"]["name"] == "k8s-credential-provider-secret-reader"
    assert rb["subjects"] == [{"kind": "ServiceAccount", "name": "k8s-credential-provider", "namespace": "ate-system"}]
    policy = _in(docs, "CiliumNetworkPolicy", _REVIEW_NS, "substrate-workers")["spec"]
    for direction, peer_key, peer in (("ingress", "fromEndpoints", "atenet-router"), ("egress", "toEndpoints", "atenet-egress")):
        assert policy[direction] == [{
            peer_key: [{"matchLabels": {"k8s:io.kubernetes.pod.namespace": "ate-system", "app": peer}}],
            "toPorts": [{"ports": [{"port": "8443", "protocol": "TCP"}]}],
        }]


def test_broker_keeps_apps_subjects_and_secret_writes_separate() -> None:
    docs = _render()
    config = _config(docs, "factory-broker")
    assert config["tokenReview"]["reviewerSubjects"] == [_SUBJECT]
    assert _SUBJECT not in config["tokenReview"]["connectorSubjects"] + config["tokenReview"]["adminSubjects"]
    assert config["reviewBindings"] == [{"name": "homelab", "repository": "jomcgi-org/homelab", "namespace": _REVIEW_NS}]
    assert config["bindings"] == [{"name": "homelab", "repository": "jomcgi-org/homelab", "namespace": "factory-r-homelab"}]
    assert set(config["actorJwt"]["allowedAtespaces"]) == {"factory-r-homelab", _REVIEW_NS}
    assert config["githubReview"] == {
        "appIdFile": "/etc/factory-review-github-app/app-id",
        "installationIdFile": "/etc/factory-review-github-app/installation-id",
        "privateKeyFile": "/etc/factory-review-github-app/private-key",
    }
    item = _in(docs, "OnePasswordItem", "factory", "factory-review-github-app")
    assert item["spec"]["itemPath"] == "vaults/k8s-homelab/items/factory-review-gh-app"
    pod = _pod(docs, "factory-broker")
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["review-github-app"]["secret"] == {"secretName": "factory-review-github-app"}
    mounts = {m["name"]: m for m in pod["containers"][0]["volumeMounts"]}
    assert mounts["review-github-app"] == {"name": "review-github-app", "mountPath": "/etc/factory-review-github-app", "readOnly": True}
    role = _in(docs, "Role", _REVIEW_NS, "factory-broker")
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create"]},
        {"apiGroups": [""], "resources": ["secrets"], "resourceNames": ["github-git"], "verbs": ["get", "update"]},
    ]
    rb = _in(docs, "RoleBinding", _REVIEW_NS, "factory-broker")
    assert rb["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "factory-broker"}
    assert rb["subjects"] == [{"kind": "ServiceAccount", "name": "factory-broker", "namespace": "factory"}]
    # Review can run independently of author delivery.
    independent = _config(_render(["broker.githubApp.enabled=false", "connector.enabled=false"]), "factory-broker")
    assert "github" not in independent
    assert independent["githubReview"] == config["githubReview"]


def test_only_declared_network_paths_and_peers_admit_the_pod() -> None:
    docs = _render()
    deploy = _in(docs, "Deployment", "factory", "factory-reviewer")
    labels = deploy["spec"]["template"]["metadata"]["labels"]
    spec = _in(docs, "CiliumNetworkPolicy", "factory", "factory-reviewer")["spec"]
    assert spec["endpointSelector"]["matchLabels"].items() <= labels.items()
    assert "ingress" not in spec
    assert len(spec["egress"]) == 3
    for rule, ns, name, port in (
        (spec["egress"][0], "factory", "factory-broker", "8081"),
        (spec["egress"][1], "kagent", "kagent-controller", "8083"),
    ):
        assert rule["toPorts"] == [{"ports": [{"port": port, "protocol": "TCP"}]}]
        peer = _in(docs, "CiliumNetworkPolicy", ns, name)["spec"]
        assert rule["toEndpoints"] == [{"matchLabels": {"k8s:io.kubernetes.pod.namespace": ns, **peer["endpointSelector"]["matchLabels"]}}]
        assert any(
            entry["toPorts"] == rule["toPorts"]
            and any(
                endpoint.get("matchLabels") == {"k8s:io.kubernetes.pod.namespace": "factory", **spec["endpointSelector"]["matchLabels"]}
                for endpoint in entry["fromEndpoints"]
            )
            for entry in peer["ingress"]
        )
    assert spec["egress"][2] == {
        "toFQDNs": [{"matchName": "api.github.com"}],
        "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}],
    }
    off = _render(["networkPolicy.cilium=false"])
    assert not any(d["kind"] == "CiliumNetworkPolicy" for d in off)


def test_service_identity_and_endpoints_follow_namespace_and_port_values() -> None:
    docs = _render([
        "connector.enabled=false",
        "itsaplan.enabled=false",
        "broker.namespace=other-factory",
        "broker.adminPort=8181",
        "kagent.namespaceOverride=other-kagent",
        "kagent.fullnameOverride=other-kagent",
        "kagent.controller.service.ports.port=8183",
        "kagent.controller.service.ports.targetPort=8183",
        "kagent.rbac.namespaces={other-kagent,factory-r-homelab,factory-r-homelab-review}",
    ])
    config = yaml.safe_load(
        _in(docs, "ConfigMap", "other-factory", "factory-reviewer")["data"]["config.yaml"]
    )
    assert config["brokerURL"] == "http://factory-broker.other-factory.svc:8181"
    assert config["kagentURL"] == "http://other-kagent-controller.other-kagent.svc:8183"
    broker = yaml.safe_load(
        _in(docs, "ConfigMap", "other-factory", "factory-broker")["data"]["config.yaml"]
    )
    assert broker["tokenReview"]["reviewerSubjects"] == [
        "system:serviceaccount:other-factory:factory-reviewer"
    ]
    rules = _in(docs, "CiliumNetworkPolicy", "other-factory", "factory-reviewer")["spec"]["egress"]
    for rule, ns, port in (
        (rules[0], "other-factory", "8181"),
        (rules[1], "other-kagent", "8183"),
    ):
        assert rule["toEndpoints"][0]["matchLabels"]["k8s:io.kubernetes.pod.namespace"] == ns
        assert rule["toPorts"][0]["ports"] == [{"port": port, "protocol": "TCP"}]


def test_custom_namespaces_multiple_repositories_and_policy_overrides() -> None:
    docs = _render(_TWO_REPOSITORIES + [
        "reviewer.bindings.homelab.namespace=custom-review",
        "reviewer.bindings.homelab.requiredChecks[0].name=pr-checks",
        "reviewer.bindings.homelab.requiredChecks[0].statusCreator=buildbuddy-io[bot]",
        "reviewer.bindings.homelab.enqueueAuthors=[]",
        "reviewer.bindings.second.humanApprovers={another-human}",
        "kagent.rbac.namespaces={kagent,factory-r-homelab,custom-review,factory-r-second,factory-r-second-review}",
    ])
    config = _config(docs, "factory-reviewer")
    assert len(config["bindings"]) == 2
    first, second = config["bindings"]
    assert first["kagentNamespace"] == "custom-review"
    assert first["enqueueAuthors"] == []
    assert first["requiredChecks"] == [{"name": "pr-checks", "statusCreator": "buildbuddy-io[bot]"}]
    assert second["kagentNamespace"] == "factory-r-second-review"
    assert second["baseBranch"] == "trunk"
    assert second["humanApprovers"] == ["another-human"]
    for binding in config["bindings"]:
        assert "namespace" not in binding
        ns = binding["kagentNamespace"]
        _in(docs, "Agent", ns, binding["kagentAgent"])
        _in(docs, "RoleBinding", ns, "factory-broker")
    assert [b["namespace"] for b in _config(docs, "factory-broker")["reviewBindings"]] == ["custom-review", "factory-r-second-review"]


@pytest.mark.parametrize("override", [
    "reviewer.bindings.homelab.namespace=factory-r-second",
    "reviewer.bindings.second.namespace=factory-r-homelab-review",
])
def test_no_cross_repository_namespace_overlap(override: str) -> None:
    with pytest.raises(RuntimeError, match="must be separate"):
        _render(_TWO_REPOSITORIES + [override])


def test_long_default_namespace_requires_an_explicit_review_namespace() -> None:
    coding_namespace = "r" * 60
    overrides = [
        "repositories[0].name=homelab",
        "repositories[0].owner=jomcgi-org",
        "repositories[0].repo=homelab",
        "repositories[0].baseBranch=main",
        f"repositories[0].namespace={coding_namespace}",
        "repositories[0].agent=codex",
        f"kagent.rbac.namespaces={{kagent,{coding_namespace},short-review}}",
    ]
    with pytest.raises(RuntimeError, match="at most 63 characters"):
        _render(overrides)
    docs = _render(overrides + ["reviewer.bindings.homelab.namespace=short-review"])
    _in(docs, "Agent", "short-review", "homelab-reviewer")


@pytest.mark.parametrize("overrides", [
    [],
    [
        "reviewer.bindings.homelab.enqueueAuthors=[]",
        "reviewer.bindings.homelab.requiredChecks[0].name=pr-checks",
        "reviewer.bindings.homelab.requiredChecks[0].statusCreator=buildbuddy-io[bot]",
        "reviewer.intervals.poll=10s",
        "reviewer.intervals.taskPoll=5s",
        "reviewer.intervals.reissue=20s",
        "reviewer.intervals.timeout=30m",
        "reviewer.intervals.retryBackoff=1m",
        "reviewer.metricsPort=9191",
    ],
])
def test_service_strict_parser_accepts_rendered_config(overrides: list[str]) -> None:
    checker = os.environ.get("CONFIG_CHECK_BIN")
    if not checker:
        pytest.skip("Bazel supplies the service config parser via CONFIG_CHECK_BIN")
    config = _config(_render(overrides), "factory-reviewer")
    result = subprocess.run(
        [checker], input=yaml.safe_dump(config), capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["AppID"] == 5758326
    assert parsed["GitHubURL"] == "https://api.github.com"
    (binding,) = parsed["Bindings"]
    assert binding["HumanApprovers"] == ["jomcgi"]
    assert binding["HumanGateLabels"] == ["human", "needs-human"]
    assert "projects/factory/reviewer/**" in binding["HumanGatePaths"]
    assert binding["MaxDiffLines"] == 2500
    assert binding["MaxDiffFiles"] == 150
    assert binding["EnqueueAuthors"] == ([] if overrides else ["jomcgi"])


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ("broker.enabled=false", "broker.enabled"),
        ("repositories=null", "repository binding"),
        ("kagent.config.server.auth.serviceAccount=null", "serviceAccount.authority"),
        ("kagent.rbac.namespaces={kagent,factory-r-homelab}", "kagent.rbac.namespaces"),
        ("reviewer.bindings.homelab.namespace=factory-r-homelab", "must be separate"),
        ("reviewer.bindings.homelab.namespace=factory", "must be separate"),
        ("reviewer.bindings.homelab.namespace=kagent", "must be separate"),
        ("reviewer.bindings.missing.namespace=review", "names no repository"),
        ("reviewer.bindings.homelab.kagentAgent=homelab-codex", "kagentAgent"),
        ("reviewer.bindings.homelab.requiredChecks[0].name=factory review,reviewer.bindings.homelab.requiredChecks[0].appId=5758326", "cannot also be a required check"),
        ("agents.reviewer.resources.memory=1Gi", "runtimeDefaults"),
        ("broker.operatorServiceAccount=factory-reviewer", "ServiceAccount the chart's Pods use"),
    ],
)
def test_invalid_wiring_fails_closed(override: str, message: str) -> None:
    with pytest.raises(RuntimeError, match=re.escape(message)):
        _render([override])
