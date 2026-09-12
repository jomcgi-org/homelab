"""Rendered guards for private-monolith destination-scoped egress (#3897).

The Cilium policy is deliberately default-off. Audit mode must remain additive,
while enforce mode must select the app pod for default-deny and permit only the
exact endpoint, entity, FQDN, and port inventory declared here. GKE carries a
default-off native policy template with the same internal inventory and the
documented public-HTTPS residual. A missing dependency causes a silent dial
timeout; a broad destination repairs that outage by reopening the compromise
path this policy exists to close. Both changes must therefore fail in CI
instead of being accepted as harmless allowlist cleanup.
"""

from __future__ import annotations

import ast
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

POLICY_SUFFIX = "-app-egress"
NATIVE_POLICY_SUFFIX = "-app-egress-native"
DNS_PORTS = (("53", "TCP"), ("53", "UDP"))
EXPECTED_EXTERNAL_FQDNS = {
    "7c56b458cd657d96b095c63d181c051f.r2.cloudflarestorage.com",
    "api.deepseek.com",
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
SCHEDULER_REGISTRATION_SOURCES = (
    "campsites/__init__.py",
    "chat/summarizer.py",
    "dr_jobs/__init__.py",
    "grimoire/__init__.py",
    "hikes/__init__.py",
    "home/__init__.py",
    "home/observability/rollup.py",
    "ships/__init__.py",
    "stars/__init__.py",
    "worldcup/__init__.py",
)


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


def _network_policies(docs: list[dict]) -> list[dict]:
    return [doc for doc in docs if doc.get("kind") == "NetworkPolicy"]


def _policy(docs: list[dict], release: str) -> dict:
    name = f"{release}{POLICY_SUFFIX}"
    matches = [doc for doc in _cnps(docs) if doc["metadata"]["name"] == name]
    assert len(matches) == 1, f"expected one {name}, found {len(matches)}"
    return matches[0]


def _native_policy(docs: list[dict], release: str) -> dict:
    name = f"{release}{NATIVE_POLICY_SUFFIX}"
    matches = [
        doc for doc in _network_policies(docs) if doc["metadata"]["name"] == name
    ]
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


def _native_endpoint_rules(policy: dict) -> set[tuple]:
    rules: set[tuple] = set()
    for rule in policy["spec"]["egress"]:
        ports = tuple(
            sorted(
                (str(port["port"]), port["protocol"]) for port in rule.get("ports", [])
            )
        )
        for destination in rule.get("to", []):
            if "podSelector" not in destination:
                continue
            namespace = destination["namespaceSelector"]["matchLabels"][
                "kubernetes.io/metadata.name"
            ]
            labels = destination["podSelector"]["matchLabels"]
            rules.add((namespace, tuple(sorted(labels.items())), ports))
    return rules


def _registered_job_names() -> set[str]:
    names: set[str] = set()
    source_root = _chart_dir().parent
    for relative_path in SCHEDULER_REGISTRATION_SOURCES:
        source_path = source_root / relative_path
        tree = ast.parse(source_path.read_text())
        register_aliases = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "scheduler.api"
            for alias in node.names
            if alias.name == "register_job"
        }
        if not register_aliases:
            continue
        for node in ast.walk(tree):
            if (
                not isinstance(node, ast.Call)
                or not isinstance(node.func, ast.Name)
                or node.func.id not in register_aliases
            ):
                continue
            name = next(
                (
                    keyword.value.value
                    for keyword in node.keywords
                    if keyword.arg == "name"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ),
                None,
            )
            if name:
                names.add(name)
    return names


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


def _native_enabled() -> dict:
    """Enable the native template with inactive-profile render fixtures."""
    return {
        "networkPolicy": {
            "egress": {
                "enabled": True,
                "apiServerCidrs": ["10.10.0.2/32"],
                "dnsServiceCidrs": ["10.10.16.10/32"],
            }
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


def test_shipped_overlays_render_only_their_supported_egress_policy():
    disabled_cases = [
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
            "monolith-dev",
            "monolith-dev",
            [_chart_dir() / "values.yaml", _recovery_gke_values()],
        ),
    ]
    for release, namespace, values in disabled_cases:
        cilium_names = {
            doc["metadata"]["name"]
            for doc in _cnps(_render(release, namespace, values))
        }
        native_names = {
            doc["metadata"]["name"]
            for doc in _network_policies(_render(release, namespace, values))
        }
        assert f"{release}{POLICY_SUFFIX}" not in cilium_names
        assert f"{release}{NATIVE_POLICY_SUFFIX}" not in native_names

    gke_docs = _render(
        "monolith",
        "monolith",
        [_chart_dir() / "values.yaml", _deploy_values(), _gke_values()],
    )
    cilium_names = {doc["metadata"]["name"] for doc in _cnps(gke_docs)}
    native_names = {doc["metadata"]["name"] for doc in _network_policies(gke_docs)}
    assert "monolith-app-egress" not in cilium_names
    assert "monolith-app-egress-native" not in native_names


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
    assert (
        policy["spec"]["endpointSelector"]["matchLabels"]["app.kubernetes.io/instance"]
        == "monolith-dev"
    )


def test_external_egress_is_exact_fqdn_on_https_only():
    policy = _policy(_prod("enforce"), "monolith")
    values = yaml.safe_load((_chart_dir() / "values.yaml").read_text())
    expected_names = set(values["ciliumPolicy"]["egress"]["externalFqdns"])
    assert expected_names == EXPECTED_EXTERNAL_FQDNS
    assert _fqdn_rules(policy) == {(name, (("443", "TCP"),)) for name in expected_names}
    assert all("*" not in name for name in expected_names)


def test_in_pod_scheduler_extract_destination_is_allowlisted():
    """Pin the external half of the register_job minus replaces audit.

    The source inventory leaves only grimoire.load_chunks and
    grimoire.extract_entities in the app pod. The loader uses internal S3 and
    embeddings. Extraction dials the deploy-configured URL, so a provider
    change must update the exact Cilium allowlist in the same change.
    """
    chart_values = yaml.safe_load((_chart_dir() / "values.yaml").read_text())
    deploy_values = yaml.safe_load(_deploy_values().read_text())
    replacements = {
        job.get("replaces")
        for job in chart_values["jobs"]["cronWorkflows"]
        if job.get("replaces")
    }
    assert _registered_job_names() - replacements == {
        "grimoire.load_chunks",
        "grimoire.extract_entities",
    }

    extract_host = urlsplit(deploy_values["grimoire"]["extractBaseUrl"]).hostname
    assert extract_host == "api.deepseek.com"
    assert extract_host in chart_values["ciliumPolicy"]["egress"]["externalFqdns"]


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


def test_gke_native_policy_template_denies_cluster_egress_and_keeps_required_flows():
    docs = _render(
        "monolith",
        "monolith",
        [_chart_dir() / "values.yaml", _deploy_values(), _gke_values()],
        _native_enabled(),
    )
    policy = _native_policy(docs, "monolith")
    assert policy["spec"]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "monolith",
        "app.kubernetes.io/instance": "monolith",
        "app.kubernetes.io/component": "app",
    }
    assert policy["spec"]["policyTypes"] == ["Egress"]
    assert policy["spec"]["egress"]

    ip_blocks = [
        (destination["ipBlock"], rule["ports"])
        for rule in policy["spec"]["egress"]
        for destination in rule.get("to", [])
        if "ipBlock" in destination
    ]
    assert ip_blocks == [
        (
            {"cidr": "10.10.16.10/32"},
            [
                {"protocol": "UDP", "port": 53},
                {"protocol": "TCP", "port": 53},
            ],
        ),
        (
            {"cidr": "10.10.0.2/32"},
            [{"protocol": "TCP", "port": 443}],
        ),
        (
            {
                "cidr": "0.0.0.0/0",
                "except": [
                    "0.0.0.0/8",
                    "10.0.0.0/8",
                    "100.64.0.0/10",
                    "127.0.0.0/8",
                    "169.254.0.0/16",
                    "172.16.0.0/12",
                    "192.168.0.0/16",
                    "224.0.0.0/4",
                    "240.0.0.0/4",
                ],
            },
            [{"protocol": "TCP", "port": 443}],
        ),
    ]

    expected_endpoints = {
        (
            namespace,
            tuple(sorted((key.removeprefix("k8s:"), value) for key, value in labels)),
            ports,
        )
        for namespace, labels, ports in _expected_endpoints(
            "monolith", "monolith", "embervm"
        )
    }
    assert _native_endpoint_rules(policy) == expected_endpoints


def test_native_policy_rejects_dns_selector_without_cilium_prefix():
    override = _native_enabled()
    override["ciliumPolicy"] = {
        "egress": {
            "targets": {
                "dns": {
                    "matchLabels": {
                        "k8s:k8s-app": None,
                        "k8s-app": "kube-dns",
                    }
                }
            }
        }
    }
    with pytest.raises(RuntimeError, match="k8s:k8s-app"):
        _render(
            "monolith",
            "monolith",
            [_chart_dir() / "values.yaml", _deploy_values(), _gke_values()],
            override,
        )


def test_egress_gate_does_not_change_existing_api_ingress():
    disabled = _render(
        "monolith",
        "monolith",
        [_chart_dir() / "values.yaml", _deploy_values()],
        {"ciliumPolicy": {"egress": {"enabled": False}}},
    )
    enabled = _prod("enforce")

    def ingress(docs: list[dict]) -> dict:
        matches = [
            doc
            for doc in _cnps(docs)
            if doc["metadata"]["name"] == "monolith-api-ingress"
        ]
        assert len(matches) == 1
        return matches[0]

    assert ingress(enabled) == ingress(disabled)


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
