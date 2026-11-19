# invariant: PLAT-2
"""Every application signs in; only kagent receives the edge access token.

The kube and explorer API routes validate their respective bearer tokens."""

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
    assert policy["oidc"].get("disableTokenEncryption", False) is False
    cookie = policy["oidc"]["cookieNames"]["idToken"]
    issuer = policy["oidc"]["provider"]["issuer"]
    assert policy["jwt"]["providers"] == [
        {
            "name": "authentik",
            "issuer": issuer,
            "audiences": [policy["oidc"]["clientID"]],
            "remoteJWKS": {"uri": issuer + "jwks/", "cacheDuration": "300s"},
            "extractFrom": {"cookies": [cookie]},
        }
    ]
    assert policy["authorization"] == {
        "defaultAction": "Deny",
        "rules": [
            {
                "name": "allow-operators",
                "action": "Allow",
                "principal": {
                    "jwt": {
                        "provider": "authentik",
                        "claims": [
                            {
                                "name": "groups",
                                "valueType": "StringArray",
                                "values": ["authentik Admins", "factory-operators"],
                            }
                        ],
                    }
                },
            }
        ],
    }
    route_path = Path(
        os.environ.get(
            "RUNBOOKS_ROUTE",
            Path(__file__).resolve().parents[2]
            / "grafana/resources/runbooks-route.yaml",
        )
    )
    route = yaml.safe_load(route_path.read_text())
    assert route["metadata"]["namespace"] in admitted
    assert route["spec"]["hostnames"] == ["factory-runbooks.jomcgi.dev"]
    # HOME-338 criterion 1: the runbook site refuses anyone without an
    # `authentik Admins` sign-in. The route attaches to the edge Gateway, so the
    # Gateway policy above (default Deny, allow only that group) covers it.
    # A route-level SecurityPolicy would replace that policy, and none targets
    # this route. Edge admission lets only the edge namespace create one.
    assert route["spec"]["parentRefs"] == [
        {**policy["targetRefs"][0], "namespace": "edge"}
    ]
    assert policy["targetRefs"] == [
        {"group": "gateway.networking.k8s.io", "kind": "Gateway", "name": "edge"}
    ]
    assert policy["oidc"]["provider"]["issuer"].startswith(
        "https://factory-auth.jomcgi.dev/"
    )
    runbook_auth = policy["authorization"]
    assert runbook_auth["defaultAction"] == "Deny"
    assert [
        (rule["action"], claim["name"], claim["values"])
        for rule in runbook_auth["rules"]
        for claim in rule["principal"]["jwt"]["claims"]
    ] == [("Allow", "groups", ["authentik Admins", "factory-operators"])]
    for (kind, _), doc in docs.items():
        if kind == "SecurityPolicy":
            assert all(
                ref.get("name") != route["metadata"]["name"]
                or ref.get("kind") != "HTTPRoute"
                for ref in doc["spec"].get("targetRefs", [])
            )
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


def test_bosun_completion_page(docs):
    rules = docs["HTTPRoute", "authentik"]["spec"]["rules"]
    match = {"path": {"type": "Exact", "value": "/bosun/application-complete"}}
    (rule,) = [r for r in rules if match in r["matches"]]
    ref = rule["filters"][0]["extensionRef"]
    response = docs[ref["kind"], ref["name"]]["spec"]["directResponse"]
    assert response["statusCode"] == 200
    assert "Bosun sign-up complete" in response["body"]["inline"]


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
        "kube-api-authentik-jwt",
        "explore-api-authentik-jwt",
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
        "jwt": gateway["jwt"],
        "authorization": gateway["authorization"],
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


def test_kube_api_requires_an_authentik_token_from_a_kube_group(docs):
    issuer = "https://factory-auth.jomcgi.dev/application/o/factory-kubectl/"
    policy = docs["SecurityPolicy", "kube-api-authentik-jwt"]
    assert policy["metadata"]["namespace"] == "edge"
    assert policy["spec"] == {
        "targetRefs": [
            {
                "group": "gateway.networking.k8s.io",
                "kind": "HTTPRoute",
                "name": "kube-api",
            }
        ],
        "jwt": {
            "providers": [
                {
                    "name": "authentik",
                    "issuer": issuer,
                    "audiences": ["factory-kubectl"],
                    "remoteJWKS": {"uri": issuer + "jwks/", "cacheDuration": "300s"},
                }
            ]
        },
        "authorization": {
            "defaultAction": "Deny",
            "rules": [
                {
                    "name": "kube-groups",
                    "action": "Allow",
                    "principal": {
                        "jwt": {
                            "provider": "authentik",
                            "claims": [
                                {
                                    "name": "groups",
                                    "valueType": "StringArray",
                                    "values": [
                                        "authentik Admins",
                                        "factory-operators",
                                        "factory-kube-viewers",
                                    ],
                                }
                            ],
                        }
                    },
                }
            ],
        },
    }
    route = docs["HTTPRoute", "kube-api"]
    assert route["metadata"]["namespace"] == "edge"
    assert route["spec"]["hostnames"] == ["factory-kube.jomcgi.dev"]
    (rule,) = route["spec"]["rules"]
    assert "filters" not in rule
    assert rule["backendRefs"] == [
        {
            "group": "",
            "kind": "Service",
            "name": "kubernetes",
            "namespace": "default",
            "port": 443,
            "weight": 1,
        }
    ]
    grant = docs["ReferenceGrant", "edge-kube-api"]
    assert grant["metadata"]["namespace"] == "default"
    assert grant["spec"]["from"] == [
        {"group": "gateway.networking.k8s.io", "kind": "HTTPRoute", "namespace": "edge"}
    ]
    assert grant["spec"]["to"] == [
        {"group": "", "kind": "Service", "name": "kubernetes"}
    ]
    tls = docs["BackendTLSPolicy", "edge-kube-api"]
    assert tls["metadata"]["namespace"] == "default"
    assert tls["spec"]["validation"] == {
        "caCertificateRefs": [
            {"group": "", "kind": "ConfigMap", "name": "kube-root-ca.crt"}
        ],
        "hostname": "kubernetes.default.svc",
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
    apiserver = [rule for rule in egress if "toEntities" in rule]
    assert apiserver == [
        {
            "toEntities": ["kube-apiserver"],
            "toPorts": [{"ports": [{"port": "6443", "protocol": "TCP"}]}],
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
        "expression": "request.namespace in ['edge', 'factory-landing'] || "
        "!has(object.spec.hostnames) || "
        "!object.spec.hostnames.exists(h, h == 'factory.jomcgi.dev')",
        "message": "Only edge and factory-landing may route factory.jomcgi.dev.",
    } in validations
    for host in (
        "factory-agents.jomcgi.dev",
        "factory-kube.jomcgi.dev",
        "factory-explore-api.jomcgi.dev",
    ):
        assert {
            "expression": "request.namespace == 'edge' || "
            "!has(object.spec.hostnames) || "
            f"!object.spec.hostnames.exists(h, h == '{host}')",
            "message": f"Only the edge namespace may route {host}.",
        } in validations
    assert (
        policies["factory-edge-envoy-resources"]["spec"]["validations"][0]["expression"]
        == "request.namespace == 'edge'"
    )
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
    assert {
        "factory-agents.jomcgi.dev",
        "factory-kube.jomcgi.dev",
        "factory-explore-api.jomcgi.dev",
    } <= set(route["spec"]["hostnames"])
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


def test_explore_api_requires_explorer_token(docs):
    group = "gateway.networking.k8s.io"
    target = [{"group": group, "kind": "HTTPRoute", "name": "explore-api"}]
    service = {"group": "", "kind": "Service", "name": "explore-proxy"}
    route = docs["HTTPRoute", "explore-api"]
    assert route["metadata"]["namespace"] == "edge"
    assert route["spec"] == {
        "parentRefs": [{"group": group, "kind": "Gateway", "name": "edge"}],
        "hostnames": ["factory-explore-api.jomcgi.dev"],
        "rules": [
            {
                "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                "timeouts": {"request": "0s", "backendRequest": "0s"},
                "backendRefs": [
                    {
                        **service,
                        "namespace": "factory-explore",
                        "port": 8080,
                        "weight": 1,
                    }
                ],
            }
        ],
    }
    grant = docs["ReferenceGrant", "edge-explore-api"]
    assert grant["metadata"]["namespace"] == "factory-explore"
    assert grant["spec"] == {
        "from": [{"group": group, "kind": "HTTPRoute", "namespace": "edge"}],
        "to": [service],
    }
    issuer = "https://factory-auth.jomcgi.dev/application/o/factory-explore/"
    assert docs["SecurityPolicy", "explore-api-authentik-jwt"]["spec"] == {
        "targetRefs": target,
        "jwt": {
            "providers": [
                {
                    "name": "authentik",
                    "issuer": issuer,
                    "audiences": ["factory-explore"],
                    "remoteJWKS": {"uri": issuer + "jwks/", "cacheDuration": "300s"},
                }
            ]
        },
        "authorization": {
            "defaultAction": "Deny",
            "rules": [
                {
                    "name": "explorers",
                    "action": "Allow",
                    "principal": {
                        "jwt": {
                            "provider": "authentik",
                            "claims": [
                                {
                                    "name": "iss",
                                    "valueType": "String",
                                    "values": [issuer],
                                }
                            ],
                        }
                    },
                }
            ],
        },
        "cors": {
            "allowOrigins": ["https://factory-explore.jomcgi.dev"],
            "allowMethods": ["GET", "POST", "OPTIONS"],
            "allowHeaders": ["Authorization", "Content-Type", "Accept"],
            "exposeHeaders": [
                "X-Explore-Window-End",
                "X-Explore-Next-Window",
                "Retry-After",
                "Warning",
            ],
            "maxAge": "600s",
        },
    }
    breaker = docs["BackendTrafficPolicy", "explore-api"]
    assert breaker["metadata"]["namespace"] == "edge"
    assert breaker["spec"] == {
        "targetRefs": target,
        "circuitBreaker": {"maxParallelRequests": 300, "maxConnections": 300},
    }
