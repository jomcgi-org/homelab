"""Every application signs in; only kagent receives the edge access token."""

import os
from pathlib import Path
import subprocess

import pytest
import yaml


@pytest.fixture(scope="module")
def docs():
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
    return {
        (d["kind"], d["metadata"]["name"]): d
        for d in yaml.safe_load_all(result.stdout)
        if d
    }


def test_edge_guards(docs):
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
    assert policy["oidc"]["clientSecret"] == {"name": "edge-oidc-client"}
    assert policy["oidc"]["provider"] == {
        "issuer": "https://factory-auth.jomcgi.dev/application/o/factory-edge/",
        "authorizationEndpoint": "https://factory-auth.jomcgi.dev/application/o/authorize/",
        "tokenEndpoint": "https://factory-auth.jomcgi.dev/application/o/token/",
        "endSessionEndpoint": "https://factory-auth.jomcgi.dev/application/o/factory-edge/end-session/",
    }
    assert policy["oidc"]["scopes"] == ["openid", "email", "profile"]
    assert policy["oidc"]["redirectURL"] == "https://%REQ(:authority)%/oauth2/callback"
    assert policy["oidc"]["logoutPath"] == "/oauth2/logout"
    assert policy["oidc"]["cookieConfig"] == {"sameSite": "Lax"}
    assert policy["oidc"]["refreshToken"] is False
    assert not policy["oidc"].get("forwardAccessToken", False)
    assert "forwardIDToken" not in policy["oidc"]
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
        "/if/user",
        "/api/v3",
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


def test_only_kagent_forwards_the_access_token(docs):
    policies = {
        name: doc for (kind, name), doc in docs.items() if kind == "SecurityPolicy"
    }
    # Additional overrides could bypass sign-in or send a foreign bearer to
    # ArgoCD, Grafana, itsaplan or the landing page.
    assert set(policies) == {
        "edge-authentik-oidc",
        "authentik-no-gateway-oidc",
        "kagent-authentik-oidc",
    }
    assert {p["metadata"]["namespace"] for p in policies.values()} == {"edge"}
    gateway = policies["edge-authentik-oidc"]["spec"]
    assert policies["kagent-authentik-oidc"]["spec"] == {
        "targetRefs": [
            {
                "group": "gateway.networking.k8s.io",
                "kind": "HTTPRoute",
                "name": "kagent",
            }
        ],
        "oidc": {**gateway["oidc"], "forwardAccessToken": True},
    }
    assert policies["authentik-no-gateway-oidc"]["spec"] == {
        "targetRefs": [
            {
                "group": "gateway.networking.k8s.io",
                "kind": "HTTPRoute",
                "name": "authentik",
            }
        ],
        "authorization": {"defaultAction": "Allow"},
    }
    route = docs["HTTPRoute", "kagent"]
    assert route["metadata"]["namespace"] == "edge"
    assert route["spec"] == {
        "parentRefs": [
            {
                "group": "gateway.networking.k8s.io",
                "kind": "Gateway",
                "name": "edge",
            }
        ],
        "hostnames": ["factory-agents.jomcgi.dev"],
        "rules": [
            {
                "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                "timeouts": {"request": "0s", "backendRequest": "0s"},
                "backendRefs": [
                    {
                        "group": "",
                        "kind": "Service",
                        "name": "kagent-ui",
                        "namespace": "kagent",
                        "port": 8080,
                        "weight": 1,
                    }
                ],
            }
        ],
    }


def test_edge_may_reference_only_the_kagent_ui(docs):
    grant = docs["ReferenceGrant", "edge-kagent-ui"]
    assert grant["metadata"]["namespace"] == "kagent"
    assert grant["spec"] == {
        "from": [
            {
                "group": "gateway.networking.k8s.io",
                "kind": "HTTPRoute",
                "namespace": "edge",
            }
        ],
        "to": [{"group": "", "kind": "Service", "name": "kagent-ui"}],
    }


def test_proxy_reaches_only_the_kagent_ui(docs):
    egress = docs["CiliumNetworkPolicy", "proxy"]["spec"]["egress"]
    kagent = [
        rule
        for rule in egress
        if any(
            endpoint.get("matchLabels", {}).get("k8s:io.kubernetes.pod.namespace")
            == "kagent"
            for endpoint in rule.get("toEndpoints", [])
        )
    ]
    assert kagent == [
        {
            "toEndpoints": [
                {
                    "matchLabels": {
                        "k8s:io.kubernetes.pod.namespace": "kagent",
                        "app.kubernetes.io/name": "kagent",
                        "app.kubernetes.io/component": "ui",
                    }
                }
            ],
            "toPorts": [{"ports": [{"port": "8080", "protocol": "TCP"}]}],
        }
    ]


def test_kagent_hostname_is_owned_and_published():
    platform = Path(__file__).resolve().parents[2]
    admission = Path(
        os.environ.get(
            "EDGE_ADMISSION", platform / "edge-admission/resources/policies.yaml"
        )
    )
    resources = list(yaml.safe_load_all(admission.read_text()))
    policies = {
        doc["metadata"]["name"]: doc
        for doc in resources
        if doc["kind"] == "ValidatingAdmissionPolicy"
    }
    validations = policies["factory-edge-routes"]["spec"]["validations"]
    assert {
        "expression": "request.namespace == 'edge' || "
        "!has(object.spec.hostnames) || "
        "!object.spec.hostnames.exists(h, h == 'factory-agents.jomcgi.dev')",
        "message": "Only the edge namespace may route factory-agents.jomcgi.dev.",
    } in validations
    assert policies["factory-edge-envoy-resources"]["spec"]["validations"][0][
        "expression"
    ] == "request.namespace == 'edge'"
    for doc in resources:
        assert doc["metadata"]["annotations"]["argocd.argoproj.io/sync-options"] == (
            "Delete=false"
        )
        if doc["kind"] == "ValidatingAdmissionPolicy":
            assert doc["spec"]["failurePolicy"] == "Fail"
        else:
            assert doc["spec"]["validationActions"] == ["Deny"]
    tunnel = Path(
        os.environ.get("EDGE_TUNNEL", platform / "edge-tunnel/resources/route.yaml")
    )
    route = yaml.safe_load(tunnel.read_text())
    assert route["metadata"]["namespace"] == "edge"
    assert "factory-agents.jomcgi.dev" in route["spec"]["hostnames"]
    assert route["spec"]["rules"] == [
        {
            "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
            "backendRefs": [
                {
                    "group": "",
                    "kind": "Service",
                    "name": "edge-proxy",
                    "port": 80,
                    "weight": 1,
                }
            ],
        }
    ]
