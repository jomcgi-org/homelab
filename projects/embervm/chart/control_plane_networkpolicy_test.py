"""Focused render regression coverage for #3824 Part A."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import yaml

_CHART_DIR = Path(__file__).resolve().parent
_ROUTER_SOURCE = Path(
    os.environ.get(
        "ROUTER_SOURCE",
        _CHART_DIR.parent / "control" / "lib" / "embervm" / "router.ex",
    )
)
_PROD_VALUES = Path(
    os.environ.get("PROD_VALUES", _CHART_DIR.parent / "deploy" / "values.yaml")
)
_GKE_VALUES = Path(
    os.environ.get("GKE_VALUES", _CHART_DIR.parent / "deploy" / "values-gke.yaml")
)
_CILIUM_VALUES = Path(
    os.environ.get(
        "CILIUM_VALUES", _CHART_DIR.parents[1] / "platform" / "cilium" / "values.yaml"
    )
)
_CILIUM_BOOTSTRAP = Path(
    os.environ.get(
        "CILIUM_BOOTSTRAP",
        _CHART_DIR.parents[1]
        / "platform"
        / "cilium"
        / "bootstrap"
        / "cilium-helmchart.yaml",
    )
)

_HTTP_PORT = "8080"
_XDS_PORT = "18000"
_HTTPV2_CONFIG = (
    "httpV2:sourceContext=workload;destinationContext=workload;"
    "labelsContext=source_namespace,destination_namespace"
)


def _render(
    *, settings: list[str] | None = None, values: list[Path] | None = None
) -> str:
    helm_bin = os.environ.get("HELM_BIN", "helm")
    argv = [helm_bin, "template", "np", str(_CHART_DIR), "--namespace", "np"]
    for value_file in values or []:
        argv += ["--values", str(value_file)]
    for setting in settings or []:
        argv += ["--set", setting]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return result.stdout


def _documents(rendered: str) -> list[dict]:
    return [
        document
        for document in yaml.safe_load_all(rendered)
        if isinstance(document, dict)
    ]


def _policy(rendered: str) -> dict:
    policies = [
        document
        for document in _documents(rendered)
        if document.get("kind") == "CiliumNetworkPolicy"
        and document.get("metadata", {}).get("name") == "np-embervm-control-plane"
    ]
    assert len(policies) == 1
    return policies[0]


def _alerts(rendered: str) -> list[dict]:
    return [
        document
        for document in _documents(rendered)
        if document.get("kind") == "ConfigMap"
        and document.get("metadata", {}).get("labels", {}).get("signoz.io/alert")
        == "true"
        and document.get("metadata", {}).get("name", "").startswith("np-embervm-http-")
    ]


def _port(rule: dict) -> str:
    ports = rule.get("toPorts", [{}])[0].get("ports", [])
    assert len(ports) == 1
    return str(ports[0]["port"])


def _http_rules(rule: dict) -> list[dict]:
    return rule["toPorts"][0]["rules"]["http"]


def _allows(rules: list[dict], method: str, path: str) -> bool:
    return any(
        ("method" not in rule or re.fullmatch(rule["method"], method))
        and re.fullmatch(rule["path"], path)
        for rule in rules
    )


def _router_routes() -> list[tuple[str, str]]:
    source = _ROUTER_SOURCE.read_text()
    routes = re.findall(
        r'^\s*(get|post|delete|put|patch)\s+"([^"]+)"', source, re.MULTILINE
    )
    return [
        (method.upper(), re.sub(r":[^/]+", "sample", path)) for method, path in routes
    ]


def test_disabled_by_default() -> None:
    rendered = _render()
    assert "np-embervm-control-plane" not in rendered
    assert _alerts(rendered) == []


def test_policy_selects_only_control_plane_and_preserves_required_ports() -> None:
    policy = _policy(_render(settings=["controlPlane.networkPolicy.enabled=true"]))
    assert policy["spec"]["endpointSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "embervm",
        "app.kubernetes.io/instance": "np",
    }

    ingress = policy["spec"]["ingress"]
    http_ingress = [rule for rule in ingress if _port(rule) == _HTTP_PORT]
    assert len(http_ingress) == 3
    assert all(
        rule["toPorts"][0].get("rules", {}).get("http") for rule in http_ingress
    ), "an L4-only allow on the HTTP port would shadow L7 enforcement"

    xds_ingress = [rule for rule in ingress if _port(rule) == _XDS_PORT]
    assert len(xds_ingress) == 1
    assert "rules" not in xds_ingress[0]["toPorts"][0]
    assert xds_ingress[0]["fromEndpoints"][0]["matchLabels"] == {
        "k8s:io.kubernetes.pod.namespace": "np",
        "app.kubernetes.io/name": "embervm-serving-envoy",
        "app.kubernetes.io/instance": "np",
        "app.kubernetes.io/component": "serving-envoy",
    }


def test_api_allow_list_covers_router_without_opening_other_session_verbs() -> None:
    policy = _policy(_render(settings=["controlPlane.networkPolicy.enabled=true"]))
    api_ingress = next(
        rule for rule in policy["spec"]["ingress"] if "fromEndpoints" in rule
    )
    rules = _http_rules(api_ingress)

    for method, path in _router_routes():
        assert _allows(rules, method, path), (
            f"router route missing from policy: {method} {path}"
        )
        if "?" not in path:
            assert _allows(rules, method, f"{path}?probe=1") or path in {
                "/healthz",
                "/livez",
            }

    for method, path in [
        ("PUT", "/v1/sessions/s-1"),
        ("POST", "/v1/sessions/s-1"),
        ("GET", "/v1/sessions/s-1/invoke"),
        ("DELETE", "/v1/sessions/s-1/invoke"),
        ("PATCH", "/v1/workloads/pi-runtime/sessions"),
        ("POST", "/v1/admin"),
    ]:
        assert not _allows(rules, method, path), (
            f"unexpected API allow: {method} {path}"
        )


def test_ancillary_wildcard_is_l7_and_scoped_to_serving_envoy() -> None:
    policy = _policy(_render(settings=["controlPlane.networkPolicy.enabled=true"]))
    wildcard = [
        rule
        for rule in policy["spec"]["ingress"]
        if _port(rule) == _HTTP_PORT
        and _http_rules(rule)
        == [
            {
                "path": "^/.*$",
                "headerMatches": [{"name": "x-ember-workload"}],
            }
        ]
    ]
    assert len(wildcard) == 1
    assert wildcard[0]["fromEndpoints"][0]["matchLabels"] == {
        "k8s:io.kubernetes.pod.namespace": "np",
        "app.kubernetes.io/name": "embervm-serving-envoy",
        "app.kubernetes.io/instance": "np",
        "app.kubernetes.io/component": "serving-envoy",
    }


def test_prod_callers_render_and_gke_overlay_disables_cilium_resources() -> None:
    prod_policy = _policy(_render(values=[_PROD_VALUES]))
    selectors = prod_policy["spec"]["ingress"][0]["fromEndpoints"]
    assert {
        "matchLabels": {
            "k8s:io.kubernetes.pod.namespace": "monolith",
            "app.kubernetes.io/name": "monolith",
            "app.kubernetes.io/component": "app",
        }
    } in selectors
    assert {
        "matchLabels": {
            "k8s:io.kubernetes.pod.namespace": "monolith-public",
            "app.kubernetes.io/name": "monolith-public",
            "app.kubernetes.io/component": "web",
        }
    } in selectors

    gke = _render(values=[_PROD_VALUES, _GKE_VALUES])
    assert "np-embervm-control-plane" not in gke
    assert _alerts(gke) == []
    gke_values = yaml.safe_load(_GKE_VALUES.read_text())
    assert gke_values["noded"]["priorityClassName"] == "homelab-preemptible"
    assert "priorityClassName" not in gke_values["controlPlane"]


def test_alerts_use_verified_hubble_series_labels_and_queries() -> None:
    rendered = _render(settings=["controlPlane.hubbleAlerts.enabled=true"])
    alerts = {
        doc["metadata"]["name"]: json.loads(doc["data"]["alert.json"])
        for doc in _alerts(rendered)
    }
    assert set(alerts) == {
        "np-embervm-http-error-rate-alert",
        "np-embervm-http-p99-latency-alert",
    }

    error_alert = alerts["np-embervm-http-error-rate-alert"]
    assert error_alert["alertType"] == "METRIC_BASED_ALERT"
    queries = error_alert["condition"]["compositeQuery"]["queries"]
    assert [query["type"] for query in queries] == [
        "builder_query",
        "builder_query",
        "builder_formula",
    ]
    assert queries[2]["spec"]["expression"] == "B / A"
    for query in queries[:2]:
        aggregation = query["spec"]["aggregations"][0]
        assert aggregation == {
            "timeAggregation": "increase",
            "spaceAggregation": "sum",
            "metricName": "hubble_http_requests_total",
        }
        expression = query["spec"]["filter"]["expression"]
        assert "destination_namespace = 'np'" in expression
        assert "destination = 'np/np-embervm'" in expression
        assert "reporter = 'server'" in expression
    assert (
        "status >= '500' AND status < '600'"
        in queries[1]["spec"]["filter"]["expression"]
    )
    assert error_alert["condition"]["selectedQueryName"] == "C"
    assert error_alert["condition"]["target"] == 0.05

    latency_alert = alerts["np-embervm-http-p99-latency-alert"]
    latency_query = latency_alert["condition"]["compositeQuery"]["queries"][0]
    assert latency_query["spec"]["aggregations"][0] == {
        "timeAggregation": "avg",
        "spaceAggregation": "p99",
        "metricName": "hubble_http_request_duration_seconds",
    }
    assert (
        "destination = 'np/np-embervm'" in latency_query["spec"]["filter"]["expression"]
    )
    assert latency_alert["condition"]["target"] == 5
    assert latency_alert["condition"]["targetUnit"] == "s"


def test_cilium_httpv2_configuration_exports_the_alert_dimensions() -> None:
    for source in (_CILIUM_VALUES, _CILIUM_BOOTSTRAP):
        text = source.read_text()
        assert _HTTPV2_CONFIG in text
        assert "hubble_http_requests_total" in text
        assert "hubble_http_request_duration_seconds" in text
