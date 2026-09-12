"""Rendered guards for private-monolith destination-scoped egress (#3897).

The policy is deliberately default-off. Audit mode must remain additive, while
enforce mode must select the app pod for default-deny and permit only the exact
endpoint, entity, FQDN, and port inventory declared here. A missing dependency
causes a silent dial timeout; a broad destination repairs that outage by
reopening the compromise path this policy exists to close. Both changes must
therefore fail in CI instead of being accepted as harmless allowlist cleanup.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

POLICY_SUFFIX = "-app-egress"
DNS_PORTS = (("53", "TCP"), ("53", "UDP"))
EXPECTED_EXTERNAL_FQDNS = {
    "7c56b458cd657d96b095c63d181c051f.r2.cloudflarestorage.com",
    "api.github.com",
    "github.com",
    "openrouter.ai",
    "integrate.api.nvidia.com",
    "api.meta.ai",
    "semgrep.dev",
    "stream.aisstream.io",
    "discord.com",
    "gateway.discord.gg",
    "cdn.discordapp.com",
    "media.discordapp.net",
    "oauth2.googleapis.com",
    "www.googleapis.com",
    "challenges.cloudflare.com",
    "jomcgi.dev",
    "private.jomcgi.dev",
    "api.met.no",
    "api.open-meteo.com",
    "geocoding-api.open-meteo.com",
    "camping.bcparks.ca",
    "www.walkhighlands.co.uk",
}


def _chart_dir() -> Path:
    here = Path(__file__).resolve().parent
    if (here / "Chart.yaml").exists():
        return here
    raise RuntimeError("Could not find chart Chart.yaml")


def _env(name: str, fallback: Path) -> Path:
    return Path(os.environ.get(name) or fallback)


def _deploy_values() -> Path:
    return _env("DEPLOY_VALUES", _chart_dir().parent / "deploy" / "values.yaml")


def _gke_values() -> Path:
    return _env("GKE_VALUES", _chart_dir().parent / "deploy" / "values-gke.yaml")


def _dev_values() -> Path:
    return _env("DEV_VALUES", _chart_dir().parent / "dev" / "deploy" / "values.yaml")


def _recovery_gke_values() -> Path:
    return _env(
        "RECOVERY_GKE_VALUES",
        _chart_dir().parent / "dev" / "deploy" / "values-recovery-gke.yaml",
    )


def _render(
    release: str,
    namespace: str,
    values: list[Path],
    override: dict | None = None,
) -> list[dict]:
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        release,
        str(_chart_dir()),
        "--namespace",
        namespace,
    ]
    for value_file in values:
        argv += ["--values", str(value_file)]

    override_path = None
    if override:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            yaml.safe_dump(override, handle)
            override_path = handle.name
        argv += ["--values", override_path]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("helm template timed out after 120s") from exc
    finally:
        if override_path:
            os.unlink(override_path)
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def _cnps(docs: list[dict]) -> list[dict]:
    return [doc for doc in docs if doc.get("kind") == "CiliumNetworkPolicy"]


def _policy(docs: list[dict], release: str) -> dict:
    name = f"{release}{POLICY_SUFFIX}"
    matches = [doc for doc in _cnps(docs) if doc["metadata"]["name"] == name]
    assert len(matches) == 1, f"expected one {name}, found {len(matches)}"
    return matches[0]


def _endpoint_rules(policy: dict) -> set[tuple]:
    rules: set[tuple] = set()
    for rule in policy["spec"]["egress"]:
        for endpoint in rule.get("toEndpoints", []):
            labels = dict(endpoint["matchLabels"])
            namespace = labels.pop("k8s:io.kubernetes.pod.namespace", None)
            ports = tuple(
                sorted(
                    (str(port["port"]), port["protocol"])
                    for block in rule.get("toPorts", [])
                    for port in block.get("ports", [])
                )
            )
            rules.add((namespace, tuple(sorted(labels.items())), ports))
    return rules


def _fqdn_rules(policy: dict) -> set[tuple[str, tuple]]:
    rules = set()
    for rule in policy["spec"]["egress"]:
        ports = tuple(
            sorted(
                (str(port["port"]), port["protocol"])
                for block in rule.get("toPorts", [])
                for port in block.get("ports", [])
            )
        )
        for destination in rule.get("toFQDNs", []):
            assert set(destination) == {"matchName"}, (
                "external egress must use exact matchName, never matchPattern"
            )
            rules.add((destination["matchName"], ports))
    return rules


def _enabled(mode: str = "audit", token_replay: bool = False) -> dict:
    return {
        "ciliumPolicy": {
            "egress": {"enabled": True, "mode": mode},
            "tokenReplayDeny": {"enabled": token_replay},
        }
    }


def _prod(mode: str = "audit", token_replay: bool = False) -> list[dict]:
    return _render(
        "monolith",
        "monolith",
        [_chart_dir() / "values.yaml", _deploy_values()],
        _enabled(mode, token_replay),
    )


def _expected_endpoints(release: str, namespace: str, embervm: str) -> set[tuple]:
    def tcp(port: int) -> tuple[tuple[str, str]]:
        return ((str(port), "TCP"),)

    return {
        ("kube-system", (("k8s:k8s-app", "kube-dns"),), DNS_PORTS),
        (namespace, (("cnpg.io/cluster", f"{release}-pg"),), tcp(5432)),
        (
            "inference",
            (
                ("app.kubernetes.io/component", "inference"),
                ("app.kubernetes.io/name", "inference"),
            ),
            tcp(8080),
        ),
        (
            "inference",
            (
                ("app.kubernetes.io/component", "embeddings"),
                ("app.kubernetes.io/name", "inference"),
            ),
            tcp(8080),
        ),
        (
            "authentik",
            (
                ("app.kubernetes.io/component", "server"),
                ("app.kubernetes.io/instance", "authentik"),
                ("app.kubernetes.io/name", "authentik"),
            ),
            tcp(9000),
        ),
        (
            "otel-collector",
            (
                ("app.kubernetes.io/instance", "otel-collector"),
                ("app.kubernetes.io/name", "otel-collector"),
            ),
            tcp(4318),
        ),
        (
            embervm,
            (
                ("app.kubernetes.io/instance", embervm),
                ("app.kubernetes.io/name", "embervm"),
            ),
            tcp(8080),
        ),
        (
            embervm,
            (
                ("app.kubernetes.io/component", "tokenbroker"),
                ("app.kubernetes.io/instance", embervm),
                ("app.kubernetes.io/name", "embervm-tokenbroker"),
            ),
            tcp(8080),
        ),
        (
            embervm,
            (
                ("app.kubernetes.io/component", "serving-envoy"),
                ("app.kubernetes.io/instance", embervm),
                ("app.kubernetes.io/name", "embervm-serving-envoy"),
            ),
            tcp(5401),
        ),
        ("gpu-operator", (("app", "nvidia-dcgm-exporter"),), tcp(9400)),
        (
            "tailscale",
            (
                ("tailscale.com/managed", "true"),
                ("tailscale.com/parent-resource", "inference-bridge-freetoken"),
                ("tailscale.com/parent-resource-ns", "tailscale"),
                ("tailscale.com/parent-resource-type", "svc"),
            ),
            tcp(8090),
        ),
        (
            namespace,
            (
                ("app.kubernetes.io/component", "searxng"),
                ("app.kubernetes.io/instance", release),
                ("app.kubernetes.io/name", "monolith"),
            ),
            tcp(8080),
        ),
    }


def test_all_shipped_overlays_keep_egress_policy_disabled():
    cases = [
        (
            "monolith",
            "monolith",
            [_chart_dir() / "values.yaml", _deploy_values()],
        ),
        (
            "monolith-dev",
            "monolith-dev",
            [_chart_dir() / "values.yaml", _deploy_values(), _dev_values()],
        ),
        (
            "monolith",
            "monolith",
            [_chart_dir() / "values.yaml", _deploy_values(), _gke_values()],
        ),
        (
            "monolith-dev",
            "monolith-dev",
            [_chart_dir() / "values.yaml", _recovery_gke_values()],
        ),
    ]
    for release, namespace, values in cases:
        names = {
            doc["metadata"]["name"]
            for doc in _cnps(_render(release, namespace, values))
        }
        assert f"{release}{POLICY_SUFFIX}" not in names


def test_audit_is_additive_and_enforce_carries_default_deny():
    audit = _policy(_prod("audit"), "monolith")
    assert audit["spec"]["enableDefaultDeny"] == {"egress": False}

    enforce = _policy(_prod("enforce"), "monolith")
    assert "enableDefaultDeny" not in enforce["spec"]
    assert enforce["spec"]["endpointSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "monolith",
        "app.kubernetes.io/instance": "monolith",
        "app.kubernetes.io/component": "app",
    }
    assert enforce["spec"]["egress"], "an empty egress list would deny required flows"


def test_enforce_pins_exact_internal_destinations_and_ports():
    policy = _policy(_prod("enforce"), "monolith")
    assert _endpoint_rules(policy) == _expected_endpoints(
        "monolith", "monolith", "embervm"
    )

    dns = [
        rule
        for rule in policy["spec"]["egress"]
        if any(
            endpoint.get("matchLabels", {}).get("k8s:k8s-app") == "kube-dns"
            for endpoint in rule.get("toEndpoints", [])
        )
    ]
    assert len(dns) == 1
    assert dns[0]["toPorts"][0]["rules"] == {"dns": [{"matchPattern": "*"}]}


def test_dev_overlay_retargets_only_its_embervm_dependencies():
    docs = _render(
        "monolith-dev",
        "monolith-dev",
        [_chart_dir() / "values.yaml", _deploy_values(), _dev_values()],
        _enabled("enforce"),
    )
    policy = _policy(docs, "monolith-dev")
    assert _endpoint_rules(policy) == _expected_endpoints(
        "monolith-dev", "monolith-dev", "embervm-dev"
    )
    assert policy["spec"]["endpointSelector"]["matchLabels"][
        "app.kubernetes.io/instance"
    ] == "monolith-dev"


def test_external_egress_is_exact_fqdn_on_https_only():
    policy = _policy(_prod("enforce"), "monolith")
    values = yaml.safe_load((_chart_dir() / "values.yaml").read_text())
    expected_names = set(values["ciliumPolicy"]["egress"]["externalFqdns"])
    assert expected_names == EXPECTED_EXTERNAL_FQDNS
    assert _fqdn_rules(policy) == {
        (name, (("443", "TCP"),)) for name in expected_names
    }
    assert all("*" not in name for name in expected_names)


def test_no_broad_cluster_or_internet_escape_hatch():
    policy = _policy(_prod("enforce"), "monolith")
    entity_rules = []
    for rule in policy["spec"]["egress"]:
        assert not rule.get("toCIDR")
        assert not rule.get("toCIDRSet")
        entities = set(rule.get("toEntities", []))
        assert not ({"all", "cluster", "world"} & entities)
        if entities:
            entity_rules.append(rule)
    assert len(entity_rules) == 1
    assert set(entity_rules[0]["toEntities"]) == {
        "kube-apiserver",
        "host",
        "remote-node",
    }
    ports = {
        (str(port["port"]), port["protocol"])
        for block in entity_rules[0]["toPorts"]
        for port in block["ports"]
    }
    assert ports == {("443", "TCP"), ("6443", "TCP")}


def test_token_replay_deny_remains_a_separate_narrow_policy():
    docs = _prod("enforce", token_replay=True)
    names = {doc["metadata"]["name"] for doc in _cnps(docs)}
    assert {"monolith-app-egress", "monolith-no-token-replay"} <= names
    deny = next(
        doc
        for doc in _cnps(docs)
        if doc["metadata"]["name"] == "monolith-no-token-replay"
    )
    assert "egressDeny" in deny["spec"]
    assert "egress" not in deny["spec"]
    assert deny["spec"]["egressDeny"][0]["toEndpoints"][0]["matchLabels"] == {
        "io.kubernetes.pod.namespace": "mcp",
        "app": "context-forge-gateway-mcp-stack-mcpgateway",
    }


def test_invalid_mode_fails_even_while_gate_is_off():
    result = subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "monolith",
            str(_chart_dir()),
            "--namespace",
            "monolith",
            "--values",
            str(_chart_dir() / "values.yaml"),
            "--set",
            "ciliumPolicy.egress.mode=observe",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0
    assert "must be audit or enforce" in result.stderr
