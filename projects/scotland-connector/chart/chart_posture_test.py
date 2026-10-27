"""Posture pins for the public connector chart: the public route reaches only
the API and MCP paths, the admin port is never exposed, ingress is admitted
only from the Envoy Gateway, the pod runs restricted, and the image deploys by
digest."""

from __future__ import annotations

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
_MONOLITH_PUBLIC_VALUES = Path(
    os.environ.get(
        "MONOLITH_PUBLIC_VALUES",
        _CHART_DIR.parent.parent / "monolith-public" / "chart" / "values.yaml",
    )
)
# Carried by :gateway_test_inputs, so it sits at the same relative path in
# Bazel runfiles as in the repository.
_GATEWAY_VALUES = (
    _CHART_DIR.parent.parent / "platform" / "cloudflare-gateway" / "values.yaml"
)
_ROUTE_ON = ["route.enabled=true", "route.hostname=connector.example.test"]


def test_aurora_activation_defaults_off_and_has_explicit_enabled_wiring() -> None:
    for overrides, expected in (([], "false"), (["config.auroraEnabled=true"], "true")):
        docs = _render(overrides)
        deployment = _one(docs, "Deployment")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        env = {item["name"]: item.get("value") for item in container["env"]}
        assert env["AURORA_ENABLED"] == expected
        assert env["STARS_AURORA_KEY"] == "aurora.json"
        assert env["STARS_BUCKET"] == "stars"


def _render(overrides: list[str] | None = None) -> list[dict[str, Any]]:
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "scotland-connector",
        str(_CHART_DIR),
        "--namespace",
        "scotland-connector",
        "--values",
        str(_DEPLOY_VALUES),
    ]
    for override in overrides or []:
        argv += ["--set", override]
    result = subprocess.run(argv, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def _one(docs: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    found = [d for d in docs if d["kind"] == kind]
    assert len(found) == 1, f"want one {kind}, got {len(found)}"
    return found[0]


def _rules(route: dict[str, Any]) -> list[tuple[str, str, dict[str, Any] | None]]:
    """(match type, match value, rewrite path or None) per rule."""
    out = []
    for rule in route["spec"]["rules"]:
        (match,) = rule["matches"]
        rewrite = None
        for f in rule.get("filters", []):
            if f["type"] == "URLRewrite":
                rewrite = f["urlRewrite"]["path"]
        out.append((match["path"]["type"], match["path"]["value"], rewrite))
    return out


def test_configured_route_serves_under_scotland_on_the_apex() -> None:
    # Rendered with the route forced on so this pins the configured hostname
    # and prefix independently of the deploy toggle.
    docs = _render(["route.enabled=true"])
    route = _one(docs, "HTTPRoute")
    assert route["metadata"]["labels"]["ingress-tier"] == "public"
    assert route["spec"]["hostnames"] == ["jomcgi.dev"]
    # Only the API and MCP paths, each stripped back to what the service serves.
    assert _rules(route) == [
        (
            "PathPrefix",
            "/scotland/v1",
            {"type": "ReplacePrefixMatch", "replacePrefixMatch": "/v1"},
        ),
        (
            "Exact",
            "/scotland/mcp",
            {"type": "ReplaceFullPath", "replaceFullPath": "/mcp"},
        ),
    ]
    # The URL the API reports as its own must be where the route serves it.
    container = _one(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env["PUBLIC_BASE_URL"] == "https://jomcgi.dev/scotland"


def test_route_without_a_prefix_serves_the_root_paths() -> None:
    docs = _render(_ROUTE_ON + ["route.pathPrefix="])
    route = _one(docs, "HTTPRoute")
    assert route["spec"]["hostnames"] == ["connector.example.test"]
    assert _rules(route) == [("PathPrefix", "/v1", None), ("Exact", "/mcp", None)]
    with pytest.raises(RuntimeError, match="pathPrefix must start with /"):
        _render(_ROUTE_ON + ["route.pathPrefix=scotland"])


def test_route_backends_are_only_the_public_service_port() -> None:
    docs = _render(["route.enabled=true"])
    service = _one(docs, "Service")
    for rule in _one(docs, "HTTPRoute")["spec"]["rules"]:
        (backend,) = rule["backendRefs"]
        assert backend["name"] == service["metadata"]["name"]
        assert backend["port"] == service["spec"]["ports"][0]["port"]


def test_route_carries_per_client_limits_and_worker_denies() -> None:
    docs = _render(_ROUTE_ON)
    policy = _one(docs, "BackendTrafficPolicy")
    fallback, assistant, per_client = policy["spec"]["rateLimit"]["local"]["rules"]
    assert "clientSelectors" not in fallback
    # One shared bucket for the assistant ranges: no Distinct.
    (assistant_match,) = assistant["clientSelectors"][0]["headers"]
    assert assistant_match["type"] == "RegularExpression"
    assert assistant["limit"]["requests"] > per_client["limit"]["requests"]
    headers = per_client["clientSelectors"][0]["headers"]
    assert [(h["name"], h["type"], h.get("invert", False)) for h in headers] == [
        ("CF-Connecting-IP", "Distinct", False),
        ("CF-Connecting-IP", "RegularExpression", False),
        ("CF-Connecting-IP", "RegularExpression", True),
    ]
    # The per-client rule excludes exactly the shared bucket's addresses.
    assert headers[2]["value"] == assistant_match["value"]
    # The identity pattern is the one monolith-public's Envoy acceptance test
    # proves; a copy that drifts from it is untested.
    monolith_public = yaml.safe_load(_MONOLITH_PUBLIC_VALUES.read_text())
    want = monolith_public["cfIngress"]["public"]["clientIdentity"]["clientHeader"][
        "valuePattern"
    ]
    assert headers[1]["value"] == want, (
        "client identity regex drifted from monolith-public"
    )
    deny = {
        r["name"]
        for r in _one(docs, "SecurityPolicy")["spec"]["authorization"]["rules"]
    }
    assert deny == {"deny-same-zone-workers", "deny-cross-zone-workers"}


def test_admin_port_is_never_exposed() -> None:
    docs = _render(_ROUTE_ON)
    service = _one(docs, "Service")
    assert [p["targetPort"] for p in service["spec"]["ports"]] == ["http"]
    container = _one(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0]
    ports = {p["name"]: p["containerPort"] for p in container["ports"]}
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert container[probe]["httpGet"]["port"] == "admin"
    policy = next(
        d
        for d in docs
        if d["kind"] == "NetworkPolicy" and d["metadata"]["name"].endswith("-ingress")
    )
    (rule,) = policy["spec"]["ingress"]
    assert [p["port"] for p in rule["ports"]] == [ports["http"]]


def test_ingress_is_admitted_only_from_the_envoy_gateway() -> None:
    docs = _render()
    policy = next(
        d
        for d in docs
        if d["kind"] == "NetworkPolicy" and d["metadata"]["name"].endswith("-ingress")
    )
    assert policy["spec"]["podSelector"] == {}
    (rule,) = policy["spec"]["ingress"]
    (peer,) = rule["from"]
    assert peer["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "envoy-gateway-system"
    }
    assert peer["podSelector"]["matchLabels"] == {
        "gateway.envoyproxy.io/owning-gateway-name": "cloudflare-ingress",
        "gateway.envoyproxy.io/owning-gateway-namespace": "envoy-gateway-system",
    }


def test_egress_policy_refuses_to_render_without_a_resolver() -> None:
    with pytest.raises(RuntimeError, match="dnsServiceCidrs"):
        _render(["networkPolicy.egress.enabled=true"])
    docs = _render(
        [
            "networkPolicy.egress.enabled=true",
            "networkPolicy.egress.dnsServiceCidrs[0]=10.0.0.10/32",
        ]
    )
    egress = next(
        d
        for d in docs
        if d["kind"] == "NetworkPolicy" and d["metadata"]["name"].endswith("-egress")
    )
    https = egress["spec"]["egress"][1]
    assert https["ports"] == [{"protocol": "TCP", "port": 443}]
    assert {"10.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16"} <= set(
        https["to"][0]["ipBlock"]["except"]
    )


def test_pod_runs_restricted_without_an_api_token() -> None:
    docs = _render()
    ns = _one(docs, "Namespace")
    assert (
        ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
    )
    pod = _one(docs, "Deployment")["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["runAsUser"] == 65532
    container = pod["containers"][0]
    sc = container["securityContext"]
    assert sc["readOnlyRootFilesystem"] is True
    assert sc["allowPrivilegeEscalation"] is False
    assert sc["capabilities"]["drop"] == ["ALL"]
    resources = container["resources"]
    assert resources["requests"]["memory"] == resources["limits"]["memory"]
    assert "cpu" not in resources["limits"]


def test_image_deploys_by_digest_when_stamped() -> None:
    digest = "sha256:" + "a" * 64
    container = _one(_render([f"image.digest={digest}"]), "Deployment")["spec"][
        "template"
    ]["spec"]["containers"][0]
    assert (
        container["image"]
        == f"ghcr.io/jomcgi/homelab/projects/scotland-connector@{digest}"
    )


def test_hpa_owns_replicas_with_a_floor_of_two() -> None:
    docs = _render()
    hpa = _one(docs, "HorizontalPodAutoscaler")
    assert hpa["spec"]["minReplicas"] >= 2
    assert (
        hpa["spec"]["scaleTargetRef"]["name"]
        == _one(docs, "Deployment")["metadata"]["name"]
    )
    assert _one(docs, "PodDisruptionBudget")["spec"]["maxUnavailable"] == 1


def test_gateway_raises_the_routes_descriptor_cap() -> None:
    # The per-client limit only holds if the gateway raises this route's
    # max_dynamic_descriptors; Envoy Gateway accepts a patch whose jsonPath
    # matches nothing, so a renamed route would silently fall back to 20.
    route = _one(_render(["route.enabled=true"]), "HTTPRoute")
    gateway = yaml.safe_load(_GATEWAY_VALUES.read_text())
    entries = [
        e
        for e in gateway["gateway"]["publicRateLimitRoutes"]
        if e["routeNamespace"] == route["metadata"]["namespace"]
        and e["routeName"] == route["metadata"]["name"]
    ]
    assert len(entries) == 1, "no publicRateLimitRoutes entry for this route"
    assert entries[0]["maxDynamicDescriptors"] >= 10000


def test_rate_limit_rules_cannot_mix_units() -> None:
    # Envoy Gateway 1.8.3 drops the route's whole local rate limit, silently,
    # when its rules use different units; the template must refuse that.
    with pytest.raises(RuntimeError, match="must equal route.rateLimit.unit"):
        _render(["route.enabled=true", "route.assistantBucket.unit=Second"])


def test_observer_is_inert_by_default_and_switch_changes_only_mcp_backend() -> None:
    baseline = _render()
    staged = _render(["mcpObserver.enabled=true"])
    routed = _render(["mcpObserver.enabled=true", "mcpObserver.routeThrough=true"])
    assert not any("mcp-observer" in d["metadata"]["name"] for d in baseline)
    assert _one(baseline, "HTTPRoute") == _one(staged, "HTTPRoute")
    expected = _one(baseline, "HTTPRoute")
    expected["spec"]["rules"][1]["backendRefs"][0]["name"] += "-mcp-observer"
    assert expected == _one(routed, "HTTPRoute")
    for kind in ("BackendTrafficPolicy", "SecurityPolicy", "HorizontalPodAutoscaler"):
        assert _one(baseline, kind) == _one(routed, kind)
    with pytest.raises(RuntimeError, match="routeThrough requires"):
        _render(["mcpObserver.routeThrough=true"])
    with pytest.raises(RuntimeError, match="requires networkPolicy.enabled"):
        _render(["mcpObserver.enabled=true", "networkPolicy.enabled=false"])


def test_observer_has_no_credentials_or_admin_service_and_only_scotland_egress() -> None:
    docs = _render(["mcpObserver.enabled=true"])
    name = "scotland-connector-mcp-observer"
    by_name = {(d["kind"], d["metadata"]["name"]): d for d in docs}
    deployment = by_name[("Deployment", name)]
    pod = deployment["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert len(pod["containers"]) == 1
    c = pod["containers"][0]
    assert not c.get("env") and not c.get("envFrom")
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["resources"]["limits"] == {"cpu": "500m", "memory": "128Mi"}
    assert c["image"].startswith("envoyproxy/envoy:v1.40.0@sha256:")
    assert pod["volumes"] == [{"name": "config", "configMap": {"name": name}}]
    assert by_name[("Service", name)]["spec"]["ports"] == [
        {"name": "http", "port": 80, "targetPort": "http"}
    ]
    # Distinct name avoids matching the connector's Service, HPA and PDB selectors.
    selector = deployment["spec"]["selector"]["matchLabels"]
    assert selector["app.kubernetes.io/name"] == "scotland-mcp-observer"
    egress = by_name[("NetworkPolicy", name + "-egress")]["spec"]
    assert egress["podSelector"]["matchLabels"] == selector
    upstream, dns = egress["egress"]
    assert upstream == {
        "to": [{"podSelector": {"matchLabels": {
            "app.kubernetes.io/name": "scotland-connector",
            "app.kubernetes.io/instance": "scotland-connector",
        }}}],
        "ports": [{"protocol": "TCP", "port": 8080}],
    }
    assert dns["to"] == [{
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
        "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
    }]
    config = yaml.safe_load(by_name[("ConfigMap", name)]["data"]["envoy.yaml"])
    assert config["admin"]["address"]["socket_address"]["address"] == "127.0.0.1"
    listener = config["static_resources"]["listeners"][0]
    limit, manager = listener["filter_chains"][0]["filters"]
    assert limit["typed_config"]["max_connections"] == 64
    hcm = manager["typed_config"]
    mcp, router = hcm["http_filters"]
    assert mcp["typed_config"] == {
        "@type": "type.googleapis.com/envoy.extensions.filters.http.mcp.v3.Mcp",
        "traffic_mode": "PASS_THROUGH", "attribute_source": "BODY",
        "max_request_body_size": 65536, "clear_route_cache": False,
        "early_terminate_when_routable": False,
    }
    routes = hcm["route_config"]["virtual_hosts"][0]["routes"]
    assert routes == [
        {"match": {"path": "/mcp"}, "route": {"cluster": "scotland", "timeout": "9s"}},
        {"match": {"prefix": "/"}, "direct_response": {"status": 404}},
    ]
    assert len(config["static_resources"]["clusters"]) == 1
    # Optional connector HTTPS egress must never broaden the observer's egress.
    enabled = _render(["mcpObserver.enabled=true", "networkPolicy.egress.enabled=true",
                       "networkPolicy.egress.dnsServiceCidrs[0]=10.0.0.10/32"])
    policy = next(d for d in enabled if d["kind"] == "NetworkPolicy"
                  and d["metadata"]["name"] == "scotland-connector-egress")
    assert policy["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "scotland-connector"
