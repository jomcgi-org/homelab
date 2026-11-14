"""Landing and ArgoCD keep the gateway sign-in and Authentik path guards."""

import os
from pathlib import Path
import subprocess

import yaml


def test_edge_guards():
    result = subprocess.run(
        [
            os.environ["HELM_BIN"],
            "template",
            "edge",
            str(Path(__file__).parent),
            "--namespace",
            "edge",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    docs = {
        (d["kind"], d["metadata"]["name"]): d
        for d in yaml.safe_load_all(result.stdout)
        if d
    }
    gateway = docs["Gateway", "edge"]["spec"]
    namespaces = gateway["listeners"][0]["allowedRoutes"]["namespaces"]
    assert namespaces["from"] == "Selector"
    admitted = namespaces["selector"]["matchExpressions"][0]["values"]
    assert {"factory-landing", "argocd", "grafana"} <= set(admitted)
    policy = docs["SecurityPolicy", "edge-authentik-oidc"]["spec"]
    assert policy["targetRefs"] == [
        {
            "group": "gateway.networking.k8s.io",
            "kind": "Gateway",
            "name": "edge",
        }
    ]
    assert policy["oidc"]["clientID"] == "factory-edge"
    sentinel = docs["HTTPRoute", "oidc-sentinel"]["spec"]
    assert sentinel["hostnames"] == ["factory.jomcgi.dev"]
    assert sentinel["rules"][0]["matches"][0]["path"]["value"] == "/oauth2-sentinel"
    assert (
        docs["HTTPRouteFilter", "oidc-sentinel"]["spec"]["directResponse"]["statusCode"]
        == 404
    )
    route = docs["HTTPRoute", "authentik"]["spec"]
    assert route["hostnames"] == ["factory-auth.jomcgi.dev"]
    blocked = {m["path"]["value"] for m in route["rules"][0]["matches"]}
    assert {
        "/if/admin",
        "/if/flow/initial-setup",
        "/api/v3/flows/executor/initial-setup",
    } <= blocked
    assert (
        route["rules"][0]["filters"][0]["extensionRef"]["name"] == "authentik-forbidden"
    )
    assert (
        docs["HTTPRouteFilter", "authentik-forbidden"]["spec"]["directResponse"][
            "statusCode"
        ]
        == 403
    )
