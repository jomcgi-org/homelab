"""Render checks for the staged direct MCP route and resource metadata."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent
MONOLITH = CHART.parent
OVERLAYS = (
    "deploy/values.yaml",
    "deploy/values-gke.yaml",
    "dev/deploy/values.yaml",
    "dev/deploy/values-recovery-gke.yaml",
)
# Match the valueFiles ordering in the prod and dev Applications. The GKE
# rendering used by the chart BUILD is checked separately from those chains.
RENDER_CASES = [
    pytest.param((), id="chart-defaults"),
    *(pytest.param((overlay,), id=overlay) for overlay in OVERLAYS),
    pytest.param(("deploy/values.yaml",), id="production-application"),
    pytest.param(
        ("deploy/values.yaml", "dev/deploy/values.yaml"), id="dev-application"
    ),
    pytest.param(("deploy/values.yaml", "deploy/values-gke.yaml"), id="production-gke"),
]


def _render(
    overlays: tuple[str, ...] = (), settings: tuple[str, ...] = ()
) -> list[dict]:
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "monolith",
        str(CHART),
        "--namespace",
        "monolith",
    ]
    for overlay in overlays:
        argv += ["-f", str(MONOLITH / overlay)]
    for setting in settings:
        argv += ["--set", setting]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return [obj for obj in yaml.safe_load_all(result.stdout) if obj]


def _object(objects: list[dict], kind: str, name: str) -> dict:
    matches = [
        obj
        for obj in objects
        if obj.get("kind") == kind and obj.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1, (kind, name, len(matches))
    return matches[0]


@pytest.mark.parametrize("overlays", RENDER_CASES)
def test_disabled_render_has_no_mcp_route_or_metadata(
    overlays: tuple[str, ...],
) -> None:
    for obj in _render(overlays):
        if obj.get("kind") not in ("HTTPRoute", "HTTPRouteFilter"):
            continue
        assert "mcp.jomcgi.dev" not in obj.get("spec", {}).get("hostnames", [])
        assert obj["metadata"]["name"] not in (
            "monolith-mcp",
            "monolith-mcp-resource-metadata",
        )


@pytest.mark.parametrize("overlay", OVERLAYS, ids=OVERLAYS)
def test_overlay_does_not_enable_mcp(overlay: str) -> None:
    values = yaml.safe_load((MONOLITH / overlay).read_text())
    mcp = values.get("cfIngress", {}).get("mcp", {})
    assert "enabled" not in mcp or mcp["enabled"] is False


def test_chart_default_is_exactly_disabled() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    assert values["cfIngress"]["mcp"]["enabled"] is False


def _assert_enabled_route(
    objects: list[dict],
    hostname: str,
    authorization_server: str,
    scopes: list[str],
) -> None:
    service = _object(objects, "Service", "monolith")
    service_name = service["metadata"]["name"]
    route_name = f"{service_name}-mcp"
    metadata_name = f"{route_name}-resource-metadata"
    metadata = _object(objects, "HTTPRouteFilter", metadata_name)
    response = metadata["spec"]["directResponse"]
    assert response["contentType"] == "application/json"
    assert response["statusCode"] == 200
    assert response["body"]["type"] == "Inline"
    assert json.loads(response["body"]["inline"]) == {
        "resource": f"https://{hostname}/mcp",
        "authorization_servers": [authorization_server],
        "bearer_methods_supported": ["header"],
        "scopes_supported": scopes,
    }

    route = _object(objects, "HTTPRoute", route_name)
    private = _object(objects, "HTTPRoute", f"{service_name}-private")
    assert route["spec"]["parentRefs"] == private["spec"]["parentRefs"]
    assert route["spec"]["hostnames"] == [hostname]
    assert "ingress-tier" not in route["metadata"].get("labels", {})
    rules = route["spec"]["rules"]
    assert len(rules) == 2
    discovery, mcp = rules
    assert discovery["matches"] == [
        {
            "path": {
                "type": "Exact",
                "value": "/.well-known/oauth-protected-resource/mcp",
            }
        }
    ]
    assert discovery["filters"] == [
        {
            "type": "ExtensionRef",
            "extensionRef": {
                "group": "gateway.envoyproxy.io",
                "kind": "HTTPRouteFilter",
                "name": metadata_name,
            },
        }
    ]
    assert mcp["matches"] == [{"path": {"type": "PathPrefix", "value": "/mcp"}}]
    api_ports = [
        port["port"] for port in service["spec"]["ports"] if port["name"] == "api"
    ]
    assert api_ports == [8000]
    assert mcp["backendRefs"] == [
        {
            "group": "",
            "kind": "Service",
            "name": service["metadata"]["name"],
            "port": api_ports[0],
            "weight": 1,
        }
    ]
    assert mcp["filters"] == [
        {
            "type": "RequestHeaderModifier",
            "requestHeaderModifier": {
                "set": [{"name": "X-Forwarded-Proto", "value": "https"}]
            },
        },
        {
            "type": "ResponseHeaderModifier",
            "responseHeaderModifier": {
                "set": [
                    {
                        "name": "WWW-Authenticate",
                        "value": (
                            f'Bearer resource_metadata="https://{hostname}'
                            '/.well-known/oauth-protected-resource/mcp"'
                        ),
                    }
                ]
            },
        },
    ]
    assert all(
        route_filter["type"] != "URLRewrite"
        for rule in rules
        for route_filter in rule.get("filters", [])
    )
    route_labels = route["metadata"].get("labels", {})
    for obj in objects:
        if obj.get("kind") != "SecurityPolicy":
            continue
        spec = obj["spec"]
        targets = spec.get("targetRefs", [])
        if "targetRef" in spec:
            targets = [*targets, spec["targetRef"]]
        assert all(target.get("name") != route_name for target in targets)
        for selector in spec.get("targetSelectors", []):
            if selector.get("kind", "HTTPRoute") != "HTTPRoute":
                continue
            assert "matchExpressions" not in selector, (
                obj["metadata"]["name"],
                selector,
            )
            match_labels = selector.get("matchLabels", {})
            assert not all(
                route_labels.get(key) == value for key, value in match_labels.items()
            ), (obj["metadata"]["name"], selector)


def test_enabled_route_uses_api_and_application_authentication() -> None:
    _assert_enabled_route(
        _render(settings=("cfIngress.mcp.enabled=true",)),
        "mcp.jomcgi.dev",
        "https://auth.jomcgi.dev/application/o/mcp-friends/",
        ["openid", "profile", "email"],
    )


def test_production_chain_advertises_the_verifier_issuer() -> None:
    chain = ("deploy/values.yaml", "deploy/values-gke.yaml")
    issuer = None
    authorization_server = None
    for values_file in [CHART / "values.yaml", *(MONOLITH / o for o in chain)]:
        values = yaml.safe_load(values_file.read_text())
        issuer = values.get("auth", {}).get("authentik", {}).get("issuer", issuer)
        authorization_server = (
            values.get("cfIngress", {})
            .get("mcp", {})
            .get("authorizationServer", authorization_server)
        )
    assert authorization_server == issuer
    objects = _render(chain, settings=("cfIngress.mcp.enabled=true",))
    service_name = _object(objects, "Service", "monolith")["metadata"]["name"]
    metadata = _object(
        objects, "HTTPRouteFilter", f"{service_name}-mcp-resource-metadata"
    )
    advertised = json.loads(metadata["spec"]["directResponse"]["body"]["inline"])
    assert advertised["authorization_servers"] == [issuer]


def test_overrides_change_metadata_route_and_challenge() -> None:
    _assert_enabled_route(
        _render(
            settings=(
                "cfIngress.mcp.enabled=true",
                "cfIngress.mcp.hostname=tools.example.test",
                "cfIngress.mcp.authorizationServer=https://identity.example.test/oauth/",
                "cfIngress.mcp.scopes={read,offline_access}",
                "cfIngress.private.gateway.name=alternate-gateway",
                "cfIngress.private.gateway.namespace=alternate-namespace",
            )
        ),
        "tools.example.test",
        "https://identity.example.test/oauth/",
        ["read", "offline_access"],
    )
