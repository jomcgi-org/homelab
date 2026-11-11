"""The friends lane serves the client-only UI and only its listed API paths."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent

# Must equal BROWSER_API_PREFIXES in projects/monolith/grimoire/browser_lane.py,
# where grimoire/browser_lane_test.py proves every routed path enforces the
# Grimoire identity. Change both together.
API_PREFIXES = [
    "/api/grimoire/lobby",
    "/api/grimoire/campaigns",
    "/api/grimoire/invitations",
    "/api/auth/platform/commands",
    "/api/auth/platform/invitations",
    "/api/auth/platform/management",
    "/api/auth/platform/self",
]


def render(*settings, deploy=False):
    command = [os.environ.get("HELM_BIN", "helm"), "template", "kg", str(CHART)]
    if deploy:
        command += [
            "-f",
            os.environ.get("DEPLOY_VALUES", str(CHART.parent / "deploy/values.yaml")),
        ]
    else:
        command += ["--set", "cfIngress.grimoire.enabled=true"]
    for value in settings:
        command += ["--set", value]
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def grimoire_route(docs):
    return next(
        doc
        for doc in docs
        if doc["kind"] == "HTTPRoute" and doc["metadata"]["name"].endswith("-grimoire")
    )


def rules_by_path(route):
    return {
        match["path"]["value"]: rule
        for rule in route["spec"]["rules"]
        for match in rule["matches"]
    }


def backend_env(docs):
    return {
        entry["name"]: entry.get("value")
        for doc in docs
        if doc["kind"] == "Deployment"
        for container in doc["spec"]["template"]["spec"]["containers"]
        if container["name"] == "backend"
        for entry in container.get("env", [])
    }


def api_port(docs):
    return next(
        port["containerPort"]
        for doc in docs
        if doc["kind"] == "Deployment"
        for container in doc["spec"]["template"]["spec"]["containers"]
        if container["name"] == "backend"
        for port in container["ports"]
        if port["name"] == "api"
    )


def test_a_disabled_lane_routes_nothing_and_sets_no_origin():
    docs = render("cfIngress.grimoire.enabled=false")
    assert not [
        doc
        for doc in docs
        if doc["kind"] == "HTTPRoute" and "grimoire" in doc["metadata"]["name"]
    ]
    assert "GRIMOIRE_BROWSER_ORIGIN" not in backend_env(docs)


@pytest.mark.parametrize("deploy", (False, True))
def test_the_lane_routes_the_ui_and_exactly_the_listed_api_prefixes(deploy):
    docs = render(deploy=deploy)
    rules = rules_by_path(grimoire_route(docs))
    assert sorted(rules) == sorted([*API_PREFIXES, "/grimoire"])
    # Never the whole API, and never the unguarded library reads.
    assert "/api/grimoire" not in rules
    # Nor the account API whole: its enrollment routes check no identity.
    assert "/api/auth/platform" not in rules
    assert not any("enrollment" in path for path in rules)
    assert not any("books" in path or "chunks" in path for path in rules)
    # Everything is served by the API port: the Svelte frontend is not on
    # this lane any more.
    for path, rule in rules.items():
        assert [ref["port"] for ref in rule["backendRefs"]] == [api_port(docs)], path
    for path in API_PREFIXES:
        headers = {
            header["name"]: header["value"]
            for item in rules[path]["filters"]
            for header in item["responseHeaderModifier"]["set"]
        }
        assert headers == {
            "Cloudflare-CDN-Cache-Control": "no-store",
            "Cache-Control": "private, no-store",
        }


@pytest.mark.parametrize("deploy", (False, True))
def test_every_rule_stays_behind_the_one_security_policy(deploy):
    docs = render(deploy=deploy)
    route = grimoire_route(docs)
    policy = next(
        doc
        for doc in docs
        if doc["kind"] == "SecurityPolicy"
        and doc["metadata"]["name"].endswith("-grimoire")
    )
    assert policy["spec"]["targetRefs"] == [
        {
            "group": "gateway.networking.k8s.io",
            "kind": "HTTPRoute",
            "name": route["metadata"]["name"],
        }
    ]
    # No other route on the hostname may answer for the API or the app.
    for doc in docs:
        if doc["kind"] != "HTTPRoute" or doc is route:
            continue
        for rule in doc["spec"].get("rules", []):
            for match in rule.get("matches", []):
                path = match.get("path", {})
                value = path.get("value", "")
                assert not any(value.startswith(prefix) for prefix in API_PREFIXES)
                if value.startswith("/grimoire"):
                    # The one anonymous path: the invitation landing, exactly.
                    assert path == {"type": "Exact", "value": "/grimoire/join"}


@pytest.mark.parametrize("deploy", (False, True))
def test_only_the_exact_invitation_landing_is_anonymous(deploy):
    docs = render(deploy=deploy)
    landing = next(
        doc
        for doc in docs
        if doc["kind"] == "HTTPRoute"
        and doc["metadata"]["name"].endswith("-grimoire-invitation")
    )
    assert [
        (match["path"], [ref["port"] for ref in rule["backendRefs"]])
        for rule in landing["spec"]["rules"]
        for match in rule["matches"]
    ] == [({"type": "Exact", "value": "/grimoire/join"}, [api_port(docs)])]
    # No login policy targets it, and it is rate limited.
    name = landing["metadata"]["name"]
    assert not [
        doc
        for doc in docs
        if doc["kind"] == "SecurityPolicy"
        and any(ref["name"] == name for ref in doc["spec"]["targetRefs"])
    ]
    assert [
        doc
        for doc in docs
        if doc["kind"] == "BackendTrafficPolicy"
        and any(ref["name"] == name for ref in doc["spec"]["targetRefs"])
        and doc["spec"]["rateLimit"]["local"]["rules"]
    ]


def test_backend_accepts_the_cookie_from_the_friends_origin_only():
    docs = render(deploy=True)
    assert backend_env(docs)["GRIMOIRE_BROWSER_ORIGIN"] == "https://friends.jomcgi.dev"
