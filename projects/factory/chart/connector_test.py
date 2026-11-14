"""Pins for the factory chart's connector: off and byte-for-byte absent under
main's deploy values, one kept identity with audience-bound tokens, bindings
derived from `repositories`, the itsaplan keys the bootstrap writes, and an
egress allowlist its peers admit."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

_CHART_DIR = Path(__file__).resolve().parent
_DEPLOY_VALUES = Path(
    os.environ.get("DEPLOY_VALUES", _CHART_DIR.parent / "deploy" / "values.yaml")
)
_RENDER_LOCK = Path(
    os.environ.get("RENDER_LOCK", _CHART_DIR.parent / "deploy" / "render.sha256")
)
_NS = "factory"
_SUBJECT = "system:serviceaccount:factory:factory-connector"
_KEEP = {
    "argocd.argoproj.io/sync-options": "Prune=false,Delete=false",
    "helm.sh/resource-policy": "keep",
}
_DB = [
    {
        "matchLabels": {
            "k8s:io.kubernetes.pod.namespace": "factory-db",
            "cnpg.io/cluster": "factory-db",
        }
    }
]
# The deploy values with the connector on, as the follow-up enablement sets
# them.
_ENABLE_VALUES = {
    "itsaplan": {"enabled": True, "database": {"endpoints": _DB}},
    "kagent": {
        "config": {"server": {"auth": {"serviceAccount": {"authority": "factory"}}}}
    },
    "connector": {
        "enabled": True,
        "bindings": {
            "homelab": {"itsaplanProject": "HOME", "agents": ["homelab-codex"]}
        },
        "database": {"endpoints": _DB},
    },
}
_ENABLE_FILE = Path(tempfile.mkdtemp()) / "enable.yaml"
_ENABLE_FILE.write_text(yaml.safe_dump(_ENABLE_VALUES))


def _helm(chart: Path, args: list[str]) -> str:
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "factory",
        str(chart),
        "--namespace",
        "kagent",
        "--values",
        str(_DEPLOY_VALUES),
        *args,
    ]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout


def _render(
    overrides: list[str] | None = None, enable: bool = True
) -> list[dict[str, Any]]:
    args = ["--values", str(_ENABLE_FILE)] if enable else []
    for override in overrides or []:
        args += ["--set", override]
    return [d for d in yaml.safe_load_all(_helm(_CHART_DIR, args)) if d]


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
    assert len(found) == 1, f"expected one {kind} {ns}/{name}, found {len(found)}"
    return found[0]


def _ours(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [d for d in docs if d["metadata"]["name"].startswith("factory-connector")]


def _pod(docs: list[dict[str, Any]]) -> dict[str, Any]:
    return _in(docs, "Deployment", _NS, "factory-connector")["spec"]["template"]


def _env(docs: list[dict[str, Any]]) -> dict[str, Any]:
    (c,) = _pod(docs)["spec"]["containers"]
    return {e["name"]: e.get("value", e.get("valueFrom")) for e in c["env"]}


def _config(docs: list[dict[str, Any]]) -> dict[str, Any]:
    cm = _in(docs, "ConfigMap", _NS, "factory-connector")
    return yaml.safe_load(cm["data"]["config.yaml"])


def _broker_subjects(docs: list[dict[str, Any]]) -> list[str]:
    cm = _in(docs, "ConfigMap", _NS, "factory-broker")
    return yaml.safe_load(cm["data"]["config.yaml"])["tokenReview"]["connectorSubjects"]


def test_deploy_values_turn_the_connector_on() -> None:
    # The factory cluster's install, with the same bindings as _ENABLE_VALUES.
    docs = _render(enable=False)
    assert _broker_subjects(docs) == [_SUBJECT]
    assert _config(docs) == _config(_render())
    _in(docs, "ClusterRoleBinding", None, "kagent-kagent-invocation-auth")


def test_disabled_renders_byte_for_byte_without_the_connector() -> None:
    # Off, the connector's templates render nothing at all.
    with tempfile.TemporaryDirectory() as tmp:
        bare = Path(tmp) / "chart"
        shutil.copytree(_CHART_DIR, bare)
        for f in ("connector.yaml", "connector-network-policy.yaml"):
            (bare / "templates" / f).unlink()
        off = ["--set", "connector.enabled=false"]
        assert _helm(_CHART_DIR, off) == _helm(bare, off)


def test_deploy_values_render_is_locked() -> None:
    # What ArgoCD applies from main's deploy values (no CRDs) changes only
    # with a deliberate update of deploy/render.sha256. The chart version
    # label is normalized, since CI writes the version back after publishing.
    rendered = re.sub(
        r"(helm\.sh/chart: factory-)\S+", r"\g<1>VERSION", _helm(_CHART_DIR, [])
    )
    got = hashlib.sha256(rendered.encode()).hexdigest()
    want = _RENDER_LOCK.read_text().strip()
    assert got == want, (
        "the deploy values' render changed; if that is intended, write the new "
        "hash to projects/factory/deploy/render.sha256: helm template factory "
        "projects/factory/chart -n kagent -f projects/factory/deploy/values.yaml "
        "| sed -E 's|(helm\\.sh/chart: factory-)[^[:space:]]+|\\1VERSION|' "
        f"| sha256sum (got {got})"
    )


def test_one_writer_by_digest_with_the_pull_secret() -> None:
    docs = _render(["connector.image.digest=sha256:" + "c" * 64])
    deploy = _in(docs, "Deployment", _NS, "factory-connector")
    assert deploy["spec"]["replicas"] == 1
    assert deploy["spec"]["strategy"] == {"type": "Recreate"}
    pod = deploy["spec"]["template"]["spec"]
    (c,) = pod["containers"]
    assert (
        c["image"]
        == "ghcr.io/jomcgi/homelab/projects/factory/connector@sha256:" + "c" * 64
    )
    assert pod["imagePullSecrets"] == [{"name": "ghcr-imagepull-secret"}]
    assert pod["serviceAccountName"] == "factory-connector"
    assert pod["automountServiceAccountToken"] is False
    # Health is served before migrations; a slow first one must not trip
    # liveness.
    assert c["startupProbe"]["httpGet"] == {"path": "/healthz", "port": "metrics"}
    assert (
        c["startupProbe"]["failureThreshold"] * c["startupProbe"]["periodSeconds"]
        >= 300
    )
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["securityContext"]["capabilities"] == {"drop": ["ALL"]}


def test_identity_is_kept() -> None:
    # A kagent principal includes the ServiceAccount's UID (finding 6).
    sa = _in(_render(), "ServiceAccount", _NS, "factory-connector")
    assert _KEEP.items() <= sa["metadata"]["annotations"].items()
    assert sa["automountServiceAccountToken"] is False


def test_tokens_are_bound_to_kagent_and_the_broker() -> None:
    def audiences(docs: list[dict[str, Any]]) -> dict[str, str]:
        out = {}
        for v in _pod(docs)["spec"]["volumes"]:
            for s in v.get("projected", {}).get("sources", []):
                if "serviceAccountToken" in s:
                    out[v["name"]] = s["serviceAccountToken"]["audience"]
        return out

    # Without the GitHub App the broker mints nothing, so delivery is off.
    docs = _render(["broker.githubApp.enabled=false"])
    assert audiences(docs) == {"kagent-token": "kagent-invocation"}
    assert "FACTORY_CONNECTOR_BROKER_URL" not in _env(docs)
    docs = _render(["broker.githubApp.enabled=true"])
    assert audiences(docs) == {
        "kagent-token": "kagent-invocation",
        "broker-token": "factory-broker",
    }
    env = _env(docs)
    assert (
        env["FACTORY_CONNECTOR_BROKER_URL"] == "http://factory-broker.factory.svc:8081"
    )
    assert (
        env["FACTORY_CONNECTOR_BROKER_TOKEN_FILE"]
        == "/var/run/secrets/factory-broker/token"
    )
    # The broker reviews for the same audience.
    broker = yaml.safe_load(
        _in(docs, "ConfigMap", _NS, "factory-broker")["data"]["config.yaml"]
    )
    assert broker["tokenReview"]["audience"] == "factory-broker"


def test_broker_admits_the_connector_subject() -> None:
    assert _broker_subjects(_render()) == [_SUBJECT]
    assert _broker_subjects(_render([f"broker.connectorSubjects={{{_SUBJECT}}}"])) == [
        _SUBJECT
    ]


def test_kagent_reviews_service_account_tokens() -> None:
    docs = _render()
    binding = _in(docs, "ClusterRoleBinding", None, "kagent-kagent-invocation-auth")
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "kagent-controller", "namespace": "kagent"}
    ]


def test_endpoints_and_secrets() -> None:
    env = _env(_render())
    assert env["FACTORY_CONNECTOR_DATABASE_URL"] == {
        "secretKeyRef": {"name": "factory-connector-db", "key": "DATABASE_URL"}
    }
    assert (
        env["FACTORY_CONNECTOR_ITSAPLAN_URL"]
        == "http://factory-itsaplan-api.factory.svc:3000"
    )
    assert (
        env["FACTORY_CONNECTOR_KAGENT_URL"]
        == "http://kagent-controller.kagent.svc:8083"
    )
    assert env["FACTORY_CONNECTOR_KAGENT_GRPC"] == "kagent-controller.kagent.svc:8083"
    assert env["FACTORY_CONNECTOR_KAGENT_TOKEN_FILE"] == "/var/run/secrets/kagent/token"
    assert env["POD_NAME"] == {"fieldRef": {"fieldPath": "metadata.name"}}


def test_itsaplan_keys_come_from_the_bootstrap_secrets() -> None:
    docs = _render()
    (keys,) = [v for v in _pod(docs)["spec"]["volumes"] if v["name"] == "itsaplan-keys"]
    # Optional: the PostSync bootstrap writes them only once this Deployment
    # is healthy, and the connector waits for the files itself.
    assert all(s["secret"]["optional"] is True for s in keys["projected"]["sources"])
    sources = {
        s["secret"]["name"]: s["secret"]["items"] for s in keys["projected"]["sources"]
    }
    assert sources == {
        "factory-itsaplan-agent-factory-coordinator": [
            {"key": "apiKey", "path": "factory-coordinator"}
        ],
        "factory-itsaplan-agent-homelab-codex": [
            {"key": "apiKey", "path": "homelab-codex"}
        ],
    }
    # The itsaplan bootstrap declares the same Secrets.
    for name in sources:
        _in(docs, "Secret", _NS, name)
    config = _config(docs)
    assert config["coordinator"] == {
        "key": "factory-coordinator",
        "apiKeyFile": "/var/run/secrets/factory-itsaplan/factory-coordinator",
    }
    assert config["agents"] == [
        {
            "key": "homelab-codex",
            "apiKeyFile": "/var/run/secrets/factory-itsaplan/homelab-codex",
            "concurrency": 1,
        }
    ]


def test_bindings_derive_from_repositories() -> None:
    docs = _render(
        [
            "connector.bindings.homelab.trustedReviewers={reviewer-bot}",
            "connector.bindings.homelab.reviewerCheck.name=semgrep",
            "connector.bindings.homelab.reviewerCheck.appId=12345",
            "connector.intervals.poll=10s",
        ]
    )
    config = _config(docs)
    assert config["bindings"] == [
        {
            "name": "homelab",
            "owner": "jomcgi-org",
            "repo": "homelab",
            "baseBranch": "main",
            "kagentNamespace": "factory-r-homelab",
            "kagentAgent": "homelab-codex",
            "itsaplanProject": "HOME",
            "agents": ["homelab-codex"],
            "trustedReviewers": ["reviewer-bot"],
            "reviewerCheck": {"name": "semgrep", "appId": 12345},
        }
    ]
    # The binding's kagent Agent is the one the chart renders.
    _in(docs, "Agent", "factory-r-homelab", "homelab-codex")
    assert config["intervals"] == {"poll": "10s"}
    assert config["links"] == {
        "issue": "https://factory-plan.jomcgi.dev/issue/{issueId}",
        "run": "",
        "session": "",
    }


def test_policy_egress_only_to_its_peers() -> None:
    def policy(docs: list[dict[str, Any]]) -> dict[str, Any]:
        return _in(docs, "CiliumNetworkPolicy", _NS, "factory-connector")["spec"]

    docs = _render(["broker.githubApp.enabled=false"])
    spec = policy(docs)
    assert (
        spec["endpointSelector"]["matchLabels"].items()
        <= _pod(docs)["metadata"]["labels"].items()
    )
    assert "ingress" not in spec
    peers = [
        (r.get("toEndpoints"), r.get("toFQDNs"), r["toPorts"][0]["ports"][0]["port"])
        for r in spec["egress"]
    ]
    assert peers == [
        (
            [
                {
                    "matchLabels": {
                        "k8s:io.kubernetes.pod.namespace": "factory",
                        "app.kubernetes.io/name": "factory-itsaplan",
                        "app.kubernetes.io/instance": "factory",
                        "app.kubernetes.io/component": "api",
                    }
                }
            ],
            None,
            "3000",
        ),
        (
            [
                {
                    "matchLabels": {
                        "k8s:io.kubernetes.pod.namespace": "kagent",
                        "app.kubernetes.io/name": "kagent",
                        "app.kubernetes.io/instance": "factory",
                        "app.kubernetes.io/component": "controller",
                    }
                }
            ],
            None,
            "8083",
        ),
        (_DB, None, "5432"),
    ]
    spec = policy(_render(["broker.githubApp.enabled=true"]))
    peers = [
        (r.get("toFQDNs"), r["toPorts"][0]["ports"][0]["port"]) for r in spec["egress"]
    ]
    assert (None, "8081") in peers
    assert ([{"matchName": "api.github.com"}], "443") in peers


def test_peers_admit_the_connector_pod() -> None:
    # kagent, itsaplan's api and the broker admit networkPolicy.connector's
    # labels (factory-db's policy names the same); the pod must carry them.
    docs = _render()
    labels = _pod(docs)["metadata"]["labels"]
    values = yaml.safe_load((_CHART_DIR / "values.yaml").read_text())
    peer = values["networkPolicy"]["connector"]
    assert peer["namespace"] == _NS
    assert peer["matchLabels"].items() <= labels.items()
    want = {"k8s:io.kubernetes.pod.namespace": _NS, **peer["matchLabels"]}
    for ns, name in (
        ("kagent", "kagent-controller"),
        (_NS, "factory-itsaplan-api"),
        (_NS, "factory-broker"),
    ):
        spec = _in(docs, "CiliumNetworkPolicy", ns, name)["spec"]
        selectors = [
            s["matchLabels"] for r in spec["ingress"] for s in r["fromEndpoints"]
        ]
        assert want in selectors, f"{ns}/{name} does not admit the connector"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ("itsaplan.enabled=false", r"connector\.enabled needs itsaplan\.enabled"),
        ("kagent.config.server.auth.serviceAccount=null", "serviceAccount.authority"),
        ("connector.bindings=null", r"connector\.bindings\.homelab is required"),
        (
            "connector.bindings.other.itsaplanProject=X,connector.bindings.other.agents={homelab-codex}",
            "names no repository",
        ),
        (
            "connector.bindings.homelab.agents={nobody}",
            "agent nobody is not in connector.agents",
        ),
        (
            "connector.bindings.homelab.agents={factory-coordinator}",
            "agent factory-coordinator is not in connector.agents",
        ),
        (
            "connector.coordinator=stranger",
            "stranger is not in itsaplan.bootstrap.agents",
        ),
        ("connector.bindings.homelab.owner=x", "owner"),
        (
            "connector.bindings.homelab.reviewerCheck.name=semgrep",
            r"reviewerCheck",
        ),
        ("connector.intervals.poll=soon", "poll"),
        ("networkPolicy.connector.namespace=elsewhere", "must be broker.namespace"),
        (
            "networkPolicy.connector.matchLabels.app\\.kubernetes\\.io/name=other",
            "does not match the connector's pod labels",
        ),
    ],
)
def test_inconsistent_values_fail(override: str, message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        _render([override])


def test_broker_is_required() -> None:
    with pytest.raises(RuntimeError, match=r"needs broker\.enabled|needs broker"):
        _render(
            ["broker.enabled=false", "repositories=null", "connector.bindings=null"]
        )
