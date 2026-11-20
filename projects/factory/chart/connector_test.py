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
    overrides: list[str] | None = None, enable: bool = True, trial: bool = False
) -> list[dict[str, Any]]:
    args = ["--values", str(_ENABLE_FILE)] if enable else []
    if trial:
        args += ["--values", str(_CHART_DIR / "files/coder-trial-values.yaml")]
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


def test_broker_uses_its_own_accounting_login() -> None:
    docs = _render()
    broker = _in(docs, "Deployment", _NS, "factory-broker")
    env = broker["spec"]["template"]["spec"]["containers"][0]["env"]
    assert next(e for e in env if e["name"] == "BROKER_USAGE_DATABASE_URL") == {
        "name": "BROKER_USAGE_DATABASE_URL",
        "valueFrom": {
            "secretKeyRef": {
                "name": "factory-broker-accounting-db",
                "key": "DATABASE_URL",
            }
        },
    }
    egress = _in(docs, "CiliumNetworkPolicy", _NS, "factory-broker")["spec"]["egress"]
    assert {
        "toEndpoints": _DB,
        "toPorts": [{"ports": [{"port": "5432", "protocol": "TCP"}]}],
    } in egress


def test_broker_accounting_secret_is_required() -> None:
    with pytest.raises(RuntimeError, match="broker.accounting.secretName is required"):
        _render(["broker.accounting.secretName="])


def test_disabled_renders_byte_for_byte_without_the_connector() -> None:
    # Off, the connector's templates render nothing at all.
    with tempfile.TemporaryDirectory() as tmp:
        bare = Path(tmp) / "chart"
        shutil.copytree(_CHART_DIR, bare)
        for f in (
            "connector.yaml",
            "connector-network-policy.yaml",
            "connector-status.yaml",
        ):
            (bare / "templates" / f).unlink()
        # The deploy values serve goal reasons, which need the connector.
        off = [
            "--set",
            "connector.enabled=false",
            "--set",
            "reviewer.goalReasons.enabled=false",
        ]
        assert _helm(_CHART_DIR, off) == _helm(bare, off)


_TRACE_ENDPOINT = "http://otel-collector.otel-collector.svc.cluster.local:4317"


def test_connector_trace_export_uses_the_factory_gateway() -> None:
    env = _env(_render([f"connector.traceEndpoint={_TRACE_ENDPOINT}"]))
    assert env["OTEL_SERVICE_NAME"] == "factory-connector"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == _TRACE_ENDPOINT
    # Export stays off in the deploy values until the gateway is live.
    for off in (_env(_render(enable=False)), _env(_render())):
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in off
        assert "OTEL_SERVICE_NAME" not in off


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
    assert env["FACTORY_CONNECTOR_KAGENT_AUTHORITY"] == "factory"
    assert env["POD_NAME"] == {"fieldRef": {"fieldPath": "metadata.name"}}


def test_caller_authority_tracks_the_controller_in_both_modes() -> None:
    for mode in ("install", "existing"):
        overrides = [
            f"kagent.mode={mode}",
            f"kagent.enabled={str(mode == 'install').lower()}",
            "kagent.controller.service.ports.port=8083",
            "kagent.config.server.auth.serviceAccount.authority=kept-controller",
        ]
        assert (
            _env(_render(overrides))["FACTORY_CONNECTOR_KAGENT_AUTHORITY"]
            == "kept-controller"
        )
        with pytest.raises(RuntimeError, match="serviceAccount.authority"):
            _render(
                [
                    f"kagent.mode={mode}",
                    f"kagent.enabled={str(mode == 'install').lower()}",
                    "kagent.controller.service.ports.port=8083",
                    "kagent.config.server.auth.serviceAccount=null",
                ]
            )


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
            "concurrency": 10,
        }
    ]


def test_single_arm_release_and_trial_drain() -> None:
    binding = _config(_render())["bindings"][0]
    assert "arms" not in binding
    assert binding["runtime"]["arm"] == "control"

    docs = _render(
        [
            "connector.bindings.homelab.arms[1].weight=0",
            "connector.bindings.homelab.arms[2].weight=0",
        ],
        trial=True,
    )
    binding = _config(docs)["bindings"][0]
    assert [a["name"] for a in binding["arms"] if a["weight"]] == ["control"]
    for arm in binding["arms"][1:]:
        name = f"{binding['kagentAgent']}-{arm['name']}"
        agent = _in(docs, "Agent", binding["kagentNamespace"], name)
        model = agent["spec"]["template"]["inline"]["modelConfig"]["name"]
        assert (
            _in(docs, "ModelConfig", binding["kagentNamespace"], model)["spec"]["model"]
            == arm["model"]
        )


def test_bindings_derive_from_repositories() -> None:
    docs = _render(
        [
            "connector.bindings.homelab.trustedReviewers={reviewer-bot}",
            "connector.bindings.homelab.reviewerCheck.name=semgrep",
            "connector.bindings.homelab.reviewerCheck.appId=12345",
            "connector.bindings.homelab.maxRebaseRounds=5",
            "connector.bindings.homelab.maxSplitRounds=2",
            "connector.intervals.poll=10s",
            "connector.intervals.quotaStall=15m",
            "connector.intervals.progressLease=25m",
            "connector.intervals.toolLease=90m",
        ],
        trial=True,
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
            "arms": [
                {
                    "name": name,
                    "model": model,
                    "reasoningEffort": effort,
                    "weight": weight,
                }
                for name, model, effort, weight in [
                    ("control", "gpt-6.2-astra", "xhigh", 70),
                    ("astra-high", "gpt-6.2-astra", "high", 15),
                    ("luna", "gpt-6-luna", "xhigh", 15),
                ]
            ],
            "runtime": {
                "model": _in(docs, "ModelConfig", "factory-r-homelab", "codex")["spec"][
                    "model"
                ],
                "reasoningEffort": "xhigh",
                "arm": "control",
                "systemPrompt": _in(
                    docs, "Agent", "factory-r-homelab", "homelab-codex"
                )["spec"]["template"]["inline"]["systemPrompt"],
            },
            "itsaplanProject": "HOME",
            "agents": ["homelab-codex"],
            "trustedReviewers": ["reviewer-bot"],
            "requiredChecks": [
                {"name": "pr-checks", "statusCreator": "buildbuddy-io[bot]"}
            ],
            "reviewerCheck": {"name": "semgrep", "appId": 12345},
            "maxReviewRounds": 6,
            "maxRebaseRounds": 5,
            "maxSplitRounds": 2,
        }
    ]
    # The binding's kagent Agent is the one the chart renders.
    _in(docs, "Agent", "factory-r-homelab", "homelab-codex")
    assert config["intervals"] == {
        "poll": "10s",
        "quotaStall": "15m",
        "progressLease": "25m",
        "toolLease": "90m",
    }
    assert config["links"] == {
        "issue": "https://factory-plan.jomcgi.dev/issue/{issueId}",
        "run": "",
        "session": "",
    }


def test_policy_egress_only_to_its_peers() -> None:
    def policy(docs: list[dict[str, Any]]) -> dict[str, Any]:
        return _in(docs, "CiliumNetworkPolicy", _NS, "factory-connector")["spec"]

    docs = _render(
        [
            "broker.githubApp.enabled=false",
            f"connector.traceEndpoint={_TRACE_ENDPOINT}",
            "reviewer.goalReasons.enabled=true",
        ]
    )
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
                        "k8s:io.kubernetes.pod.namespace": "otel-collector",
                        "app.kubernetes.io/name": "otel-collector",
                        "app.kubernetes.io/instance": "otel-collector",
                    }
                }
            ],
            None,
            "4317",
        ),
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
        (
            [
                {
                    "matchLabels": {
                        "k8s:io.kubernetes.pod.namespace": "factory",
                        "app.kubernetes.io/name": "factory-reviewer",
                        "app.kubernetes.io/instance": "factory",
                    }
                }
            ],
            None,
            "8091",
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
        (
            "itsaplan.enabled=false,reviewer.goalReasons.enabled=false",
            r"connector\.enabled needs itsaplan\.enabled",
        ),
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
            "stranger is not a writable agent in itsaplan.bootstrap.agents",
        ),
        (
            # A readOnly agent's key can never claim runs or write comments.
            "connector.coordinator=factory-storyboard",
            "factory-storyboard is not a writable agent",
        ),
        ("connector.bindings.homelab.owner=x", "owner"),
        ("connector.bindings.homelab.maxRebaseRounds=-1", "maxRebaseRounds"),
        ("connector.bindings.homelab.maxSplitRounds=-1", "maxSplitRounds"),
        (
            # The deploy values name an App; also naming a creator fails.
            "connector.bindings.homelab.reviewerCheck.statusCreator=someone",
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


def test_notify_is_off_until_configured() -> None:
    docs = _render()
    assert not any(key.startswith("FACTORY_CONNECTOR_NOTIFY_") for key in _env(docs))
    assert not any(
        v["name"] == "notify-password" for v in _pod(docs)["spec"]["volumes"]
    )
    assert not any(d["metadata"]["name"] == "factory-notify" for d in docs)


def test_notify_secret_config_and_connector_only_egress() -> None:
    docs = _render(["connector.notify.enabled=true"])
    env = _env(docs)
    assert (
        env["FACTORY_CONNECTOR_NOTIFY_URL"] == "https://factory-notify.jomcgi.dev/mcp/"
    )
    assert (
        env["FACTORY_CONNECTOR_NOTIFY_TOKEN_URL"]
        == "https://auth.jomcgi.dev/application/o/token/"
    )
    assert (
        env["FACTORY_CONNECTOR_NOTIFY_PASSWORD_FILE"]
        == "/var/run/secrets/factory-notify/app-password"
    )
    item = _in(docs, "OnePasswordItem", _NS, "factory-notify")
    assert item["spec"] == {"itemPath": "vaults/k8s-homelab/items/factory-notify"}
    pod = _pod(docs)["spec"]
    volume = next(v for v in pod["volumes"] if v["name"] == "notify-password")
    assert volume["secret"] == {
        "secretName": "factory-notify",
        "items": [{"key": "app-password", "path": "app-password"}],
    }
    assert {
        "name": "notify-password",
        "mountPath": "/var/run/secrets/factory-notify",
        "readOnly": True,
    } in pod["containers"][0]["volumeMounts"]
    policies = [d for d in docs if d["kind"] == "CiliumNetworkPolicy"]
    rules = [
        (d, r)
        for d in policies
        for r in d["spec"].get("egress", [])
        if {"matchName": "factory-notify.jomcgi.dev"} in r.get("toFQDNs", [])
    ]
    assert len(rules) == 1
    policy, rule = rules[0]
    assert policy["metadata"]["name"] == "factory-connector"
    assert policy["spec"]["endpointSelector"]["matchLabels"] == {
        k: v
        for k, v in _pod(docs)["metadata"]["labels"].items()
        if k != "app.kubernetes.io/part-of"
    }
    assert rule["toFQDNs"] == [
        {"matchName": "factory-notify.jomcgi.dev"},
        {"matchName": "auth.jomcgi.dev"},
    ]
    assert rule["toPorts"] == [{"ports": [{"port": "443", "protocol": "TCP"}]}]


@pytest.mark.parametrize(
    "override",
    [
        "connector.notify.hostname=example.com/path",
        "connector.notify.hostname=*.example.com",
        "connector.notify.tokenHostname=example.com:8080",
        "networkPolicy.cilium=false",
        "connector.notify.secretName=",
        "connector.notify.itemPath=",
    ],
)
def test_notify_rejects_incomplete_or_broad_configuration(override) -> None:
    with pytest.raises(RuntimeError):
        _render(["connector.notify.enabled=true", override])


_BUILDBUDDY = [
    "connector.buildbuddy.enabled=true",
    "connector.buildbuddy.host=example.buildbuddy.io",
    "connector.buildbuddy.itemPath=vaults/k8s-homelab/items/buildbuddy",
]


def test_buildbuddy_is_off_by_default() -> None:
    docs = _render()
    assert "buildbuddy" not in _config(docs)
    assert not [
        d
        for d in docs
        if d["kind"] == "OnePasswordItem" and "buildbuddy" in d["metadata"]["name"]
    ]
    assert "buildbuddy" not in yaml.safe_dump(_pod(docs))
    egress = _in(docs, "CiliumNetworkPolicy", _NS, "factory-connector")["spec"][
        "egress"
    ]
    assert not any("buildbuddy" in yaml.safe_dump(r) for r in egress)


def test_buildbuddy_key_and_host_belong_only_to_the_connector() -> None:
    docs = _render(_BUILDBUDDY)
    assert _config(docs)["buildbuddy"] == {
        "apiURL": "https://example.buildbuddy.io",
        "invocationPrefix": "https://example.buildbuddy.io/invocation/",
        "apiKeyFile": "/var/run/secrets/factory-buildbuddy/api-key",
    }
    item = _in(docs, "OnePasswordItem", _NS, "factory-connector-buildbuddy")
    assert item["spec"] == {"itemPath": "vaults/k8s-homelab/items/buildbuddy"}
    pod = _pod(docs)["spec"]
    (volume,) = [v for v in pod["volumes"] if v["name"] == "buildbuddy"]
    assert volume["secret"] == {
        "secretName": "factory-connector-buildbuddy",
        "optional": True,
        "items": [{"key": "API_KEY", "path": "api-key"}],
    }
    (container,) = pod["containers"]
    assert {
        "name": "buildbuddy",
        "mountPath": "/var/run/secrets/factory-buildbuddy",
        "readOnly": True,
    } in container["volumeMounts"]
    egress = _in(docs, "CiliumNetworkPolicy", _NS, "factory-connector")["spec"][
        "egress"
    ]
    assert {
        "toFQDNs": [{"matchName": "example.buildbuddy.io"}],
        "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}],
    } in egress
    # Nothing else in the chart names the key, its mount or the host.
    for doc in docs:
        if doc["metadata"]["name"] in (
            "factory-connector",
            "factory-connector-buildbuddy",
        ):
            continue
        text = yaml.safe_dump(doc)
        for needle in (
            "example.buildbuddy.io",
            "factory-connector-buildbuddy",
            "factory-buildbuddy",
        ):
            assert needle not in text, (doc["metadata"]["name"], needle)


@pytest.mark.parametrize(
    "overrides",
    [
        ["connector.buildbuddy.enabled=true"],
        _BUILDBUDDY[:2] + ["connector.buildbuddy.itemPath="],
        [_BUILDBUDDY[0], _BUILDBUDDY[2]],
        _BUILDBUDDY + ["connector.buildbuddy.host=https://example.buildbuddy.io"],
        _BUILDBUDDY + ["connector.buildbuddy.host=example.buildbuddy.io/x"],
        _BUILDBUDDY + ["connector.buildbuddy.itemPath=bad-path"],
        _BUILDBUDDY + ["connector.buildbuddy.key=a b"],
    ],
)
def test_invalid_buildbuddy_configuration_fails_render(overrides: list[str]) -> None:
    with pytest.raises(RuntimeError):
        _render(overrides)


def test_landing_status_access() -> None:
    docs = _render()
    connector_labels = {
        "app.kubernetes.io/name": "factory-connector",
        "app.kubernetes.io/instance": "factory",
    }
    ports = [{"ports": [{"port": "8082", "protocol": "TCP"}]}]
    status_service = _in(docs, "Service", _NS, "factory-connector-status")
    assert status_service["metadata"]["namespace"] == "factory"
    assert status_service["spec"]["selector"] == connector_labels
    assert status_service["spec"]["ports"] == [
        {"name": "status", "port": 8082, "targetPort": 8082, "protocol": "TCP"}
    ]
    status_policy = _in(docs, "CiliumNetworkPolicy", _NS, "factory-landing-status")
    assert status_policy["metadata"]["namespace"] == "factory"
    assert status_policy["spec"] == {
        "endpointSelector": {"matchLabels": connector_labels},
        "ingress": [
            {
                "fromEndpoints": [
                    {
                        "matchLabels": {
                            "k8s:io.kubernetes.pod.namespace": "factory-landing",
                            "app.kubernetes.io/name": "factory-landing",
                        }
                    }
                ],
                "toPorts": ports,
            }
        ],
    }
    for resource in (status_service, status_policy):
        options = resource["metadata"]["annotations"]["argocd.argoproj.io/sync-options"]
        assert "Prune=false" in options.split(",")


def test_reaper_uses_its_own_database_login():
    # The connector's login owns factory_connector; the reaper gets a login
    # limited to attempt lookup and worker-fence writes.
    docs = _render()
    reaper = _in(docs, "Deployment", "factory", "factory-reaper")
    refs = [
        env["valueFrom"]["secretKeyRef"]["name"]
        for c in reaper["spec"]["template"]["spec"]["containers"]
        for env in c.get("env", [])
        if "secretKeyRef" in env.get("valueFrom", {})
    ]
    assert refs == ["factory-reaper-db"]


def test_reaper_pod_permissions():
    docs = _render()
    namespaces = {"factory-r-homelab", "factory-r-homelab-review"}
    roles = [d for d in docs if d["metadata"]["name"] == "factory-reaper"]
    for kind in ["Role", "RoleBinding"]:
        assert {
            d["metadata"]["namespace"] for d in roles if d["kind"] == kind
        } == namespaces
    for role in (d for d in roles if d["kind"] == "Role"):
        assert role["rules"] == [
            {
                "apiGroups": [""],
                "resources": ["pods"],
                "verbs": ["get", "list", "watch", "delete"],
            }
        ]
    assert not any(d["kind"].startswith("ClusterRole") for d in roles)
    connector = _in(docs, "Deployment", "factory", "factory-connector")
    assert (
        connector["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
    )


def test_runbook_revert_permissions():
    docs = _render(["runbookRevert.enabled=true"])
    # Resolve every rendered binding, including group and cluster grants. A
    # second binding in a different template must expand this set and fail.
    roles = {
        (doc["kind"], doc["metadata"].get("namespace"), doc["metadata"]["name"]): doc
        for doc in docs
        if doc["kind"] in ("Role", "ClusterRole")
    }

    def permissions(namespace, account):
        groups = {
            "system:authenticated",
            "system:serviceaccounts",
            f"system:serviceaccounts:{namespace}",
        }
        user = f"system:serviceaccount:{namespace}:{account}"
        grants = []
        for doc in docs:
            if doc["kind"] not in ("RoleBinding", "ClusterRoleBinding"):
                continue
            ns = doc["metadata"].get("namespace")
            if not any(
                subject["kind"] == "ServiceAccount"
                and subject.get("namespace", ns) == namespace
                and subject["name"] == account
                or subject["kind"] == "Group"
                and subject["name"] in groups
                or subject["kind"] == "User"
                and subject["name"] == user
                for subject in doc.get("subjects", [])
            ):
                continue
            ref = doc["roleRef"]
            role = roles[
                ref["kind"], ns if ref["kind"] == "Role" else None, ref["name"]
            ]
            for rule in role["rules"]:
                assert rule["apiGroups"] == [""]
                grants.extend(
                    (ns, resource, name, verb)
                    for resource in rule["resources"]
                    for name in rule.get("resourceNames", ["*"])
                    for verb in rule["verbs"]
                )
        return set(grants)

    owners = {
        "factory-connector": "factory-r-homelab",
        "factory-reviewer": "factory-r-homelab-review",
    }
    readers = {
        ("grafana", "configmaps", "runbook-revert-" + owner, "get") for owner in owners
    }
    journals = readers | {
        (ns, resource, name, "update") for ns, resource, name, _ in readers
    }
    # Delete only: the action reads no pods, so it cannot list or read
    # unrelated pods in the worker namespaces.
    workers = {(ns, "pods", "*", "delete") for ns in owners.values()}
    assert permissions("grafana", "runbooks") == set()
    assert permissions("grafana", "runbook-deadline") == readers
    assert permissions("grafana", "runbook-kagent-revert") == journals | workers
    for owner, worker_ns in owners.items():
        assert permissions("factory", owner) == {
            (worker_ns, "pods", "*", "get"),
            ("grafana", "configmaps", "runbook-revert-" + owner, "get"),
            ("grafana", "configmaps", "runbook-revert-" + owner, "update"),
        }
