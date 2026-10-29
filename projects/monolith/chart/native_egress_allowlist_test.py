"""Rendered guards for private-monolith native egress (#3897).

GKE carries a default-off native policy template with exact internal
destinations and the documented public-HTTPS residual. A missing dependency
causes a silent dial timeout; a broad destination repairs that outage by reopening the compromise
path this policy exists to close. Both changes must therefore fail in CI
instead of being accepted as harmless allowlist cleanup.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

NATIVE_POLICY_SUFFIX = "-app-egress-native"
DNS_PORTS = (("53", "TCP"), ("53", "UDP"))


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


def _native_policy(docs: list[dict], release: str) -> dict:
    name = f"{release}{NATIVE_POLICY_SUFFIX}"
    matches = [
        doc for doc in _network_policies(docs) if doc["metadata"]["name"] == name
    ]
    assert len(matches) == 1, f"expected one {name}, found {len(matches)}"
    return matches[0]


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


def _expected_endpoints(release: str, namespace: str, embervm: str) -> set[tuple]:
    def tcp(port: int) -> tuple[tuple[str, str]]:
        return ((str(port), "TCP"),)

    return {
        ("kube-system", (("k8s-app", "kube-dns"),), DNS_PORTS),
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
        assert not _cnps(_render(release, namespace, values))
        native_names = {
            doc["metadata"]["name"]
            for doc in _network_policies(_render(release, namespace, values))
        }
        assert f"{release}{NATIVE_POLICY_SUFFIX}" not in native_names

    gke_docs = _render(
        "monolith",
        "monolith",
        [_chart_dir() / "values.yaml", _deploy_values(), _gke_values()],
    )
    assert not _cnps(gke_docs)
    native_names = {doc["metadata"]["name"] for doc in _network_policies(gke_docs)}
    assert "monolith-app-egress-native" not in native_names


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("profile", ["defaults", "gke"])
def test_private_qwen_configuration_and_proxy_route_are_retired(profile, enabled):
    values = [_chart_dir() / "values.yaml"]
    if profile == "gke":
        values += [_deploy_values(), _gke_values()]
    for path in values:
        config = yaml.safe_load(path.read_text())
        assert "qwenApiBase" not in config.get("frontend", {})
        targets = config.get("networkPolicy", {}).get("egress", {}).get("targets", {})
        assert "inferenceBridge" not in targets

    override = _native_enabled() if enabled else {}
    # Old frontend values must not restore the removed environment variable.
    override["frontend"] = {"qwenApiBase": "http://retired.test:8090"}
    docs = _render("monolith", "monolith", values, override)
    rendered = yaml.safe_dump_all(docs)
    for retired in ("QWEN_API_BASE", "/private/qwen", "inference-bridge-freetoken"):
        assert retired not in rendered
    routes = [doc for doc in docs if doc["kind"] == "HTTPRoute"]
    rules = {
        match["path"]["value"]: rule
        for route in routes
        for rule in route["spec"]["rules"]
        for match in rule.get("matches", [])
        if "path" in match
    }
    # The other streaming routes still need their existing long timeouts.
    for path in (
        "/private/chat",
        "/private/agents/vms/stream",
        "/private/factory/execution/vms/stream",
    ):
        assert rules[path]["timeouts"] == {
            "request": "600s",
            "backendRequest": "600s",
        }
    assert "/" in rules
    if enabled:
        assert _native_endpoint_rules(_native_policy(docs, "monolith")) == (
            _expected_endpoints("monolith", "monolith", "embervm")
        )
    else:
        assert not any(
            doc["metadata"]["name"] == f"monolith{NATIVE_POLICY_SUFFIX}"
            for doc in _network_policies(docs)
        )


@pytest.mark.parametrize("profile", ["defaults", "deploy", "gke", "dev", "recovery"])
def test_stats_exporter_is_absent_from_values_and_rendered_workloads(profile):
    values = [_chart_dir() / "values.yaml"]
    if profile in {"deploy", "gke", "dev"}:
        values.append(_deploy_values())
    if profile == "gke":
        values.append(_gke_values())
    if profile == "dev":
        values.append(_dev_values())
    if profile == "recovery":
        values.append(_recovery_gke_values())

    for path in values:
        config = yaml.safe_load(path.read_text())
        assert "dcgmExporterUrl" not in config.get("backend", {})
        targets = config.get("networkPolicy", {}).get("egress", {}).get("targets", {})
        assert "dcgmExporter" not in targets
        assert "DCGM_EXPORTER_URL" not in path.read_text()

    # Exercise the removed backend template even with an old values key.
    override = {"backend": {"dcgmExporterUrl": "http://retired-exporter.test:9400"}}
    if profile in {"defaults", "deploy", "gke"}:
        # Production injects this map at chart build time. Dev and recovery
        # intentionally suppress jobs, so keep their image gate untouched.
        override["jobs"] = {
            "image": {"repository": "registry.invalid/jobs", "tag": "test"}
        }
    docs = _render("monolith", "monolith", values, override)
    assert any(doc["kind"] == "Deployment" for doc in docs)
    if profile in {"defaults", "deploy", "gke"}:
        assert any(
            doc["kind"] == "CronWorkflow"
            and "observability-stats-rollup" in doc["metadata"]["name"]
            for doc in docs
        )
    rendered = yaml.safe_dump_all(docs)
    for retired in (
        "DCGM_EXPORTER_URL",
        "dcgmExporter",
        "nvidia-dcgm-exporter",
        "gpu-operator",
    ):
        assert retired not in rendered


def test_dev_overlay_retargets_only_its_embervm_dependencies():
    docs = _render(
        "monolith-dev",
        "monolith-dev",
        [_chart_dir() / "values.yaml", _deploy_values(), _dev_values()],
        _native_enabled(),
    )
    policy = _native_policy(docs, "monolith-dev")
    assert _native_endpoint_rules(policy) == _expected_endpoints(
        "monolith-dev", "monolith-dev", "embervm-dev"
    )
    assert (
        policy["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/instance"]
        == "monolith-dev"
    )


def test_extract_destination_uses_public_https():
    """Keep the configured extraction provider on public HTTPS."""
    deploy_values = yaml.safe_load(_deploy_values().read_text())
    extract_host = urlsplit(deploy_values["grimoire"]["extractBaseUrl"]).hostname
    assert extract_host == "api.deepseek.com"
    assert urlsplit(deploy_values["grimoire"]["extractBaseUrl"]).scheme == "https"


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
    override["networkPolicy"]["egress"]["targets"] = {
        "dns": {
            "matchLabels": {
                "k8s:k8s-app": None,
                "k8s-app": "kube-dns",
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


@pytest.mark.parametrize("missing", ["apiServerCidrs", "dnsServiceCidrs"])
def test_native_policy_requires_live_validated_addresses(missing):
    override = _native_enabled()
    override["networkPolicy"]["egress"][missing] = []
    with pytest.raises(RuntimeError, match=missing):
        _render("monolith", "monolith", [_gke_values()], override)
