"""The friends lane routes the client-only UI and only its listed API paths."""

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


def test_off_by_default_leaves_only_the_bff_reachable():
    docs = render()
    paths = set(rules_by_path(grimoire_route(docs)))
    assert paths == {"/grimoire/_app", "/grimoire"}
    assert "GRIMOIRE_BROWSER_ORIGIN" not in backend_env(docs)


@pytest.mark.parametrize("deploy", (False, True))
def test_enabled_routes_the_ui_and_exactly_the_listed_api_prefixes(deploy):
    docs = render("cfIngress.grimoire.browserUi.enabled=true", deploy=deploy)
    route = grimoire_route(docs)
    rules = rules_by_path(route)
    api = sorted(path for path in rules if path.startswith("/api"))
    assert api == sorted(API_PREFIXES)
    # Never the whole API, and never the unguarded library reads.
    assert "/api/grimoire" not in rules
    assert not any("books" in path or "chunks" in path for path in rules)

    api_port = next(
        port
        for doc in docs
        if doc["kind"] == "Deployment"
        for container in doc["spec"]["template"]["spec"]["containers"]
        if container["name"] == "backend"
        for port in [
            p["containerPort"] for p in container["ports"] if p["name"] == "api"
        ]
    )
    for path in [*API_PREFIXES, "/grimoire/next"]:
        assert [ref["port"] for ref in rules[path]["backendRefs"]] == [api_port]
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
    # The Svelte UI keeps every other path on the lane until cutover.
    assert [ref["port"] for ref in rules["/grimoire"]["backendRefs"]] != [api_port]


@pytest.mark.parametrize("deploy", (False, True))
def test_every_rule_stays_behind_the_one_security_policy(deploy):
    docs = render("cfIngress.grimoire.browserUi.enabled=true", deploy=deploy)
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
    # No other route on the hostname may answer for the UI or its API.
    for doc in docs:
        if doc["kind"] != "HTTPRoute" or doc is route:
            continue
        for rule in doc["spec"].get("rules", []):
            for match in rule.get("matches", []):
                value = match.get("path", {}).get("value", "")
                assert not value.startswith("/api/grimoire/lobby")
                assert not value.startswith("/api/grimoire/campaigns")
                assert not value.startswith("/grimoire/next")


def test_backend_accepts_the_cookie_from_the_friends_origin_only():
    docs = render("cfIngress.grimoire.browserUi.enabled=true", deploy=True)
    assert backend_env(docs)["GRIMOIRE_BROWSER_ORIGIN"] == "https://friends.jomcgi.dev"


def test_production_serves_the_ui_beside_the_svelte_pages():
    rules = rules_by_path(grimoire_route(render(deploy=True)))
    assert "/grimoire/next" in rules
    assert "/grimoire" in rules
