# invariant: PLAT-2
"""Every application signs in; only kagent receives the edge access token.

The kube and explorer API routes validate their respective bearer tokens."""

import os
from pathlib import Path
import subprocess

import pytest
import yaml

# The Discord servers with an artifact folder (values.yaml artifacts.servers).
SERVERS = [
    "185442367703220224",
    "1290668195249917964",
    "1501965852042330302",
    "1512814732392927463",
]


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
            "claimToHeaders": [{"claim": "sub", "header": "X-Runbook-User"}],
            "extractFrom": {"cookies": [cookie]},
        }
    ]
    assert policy["authorization"] == {
        "defaultAction": "Deny",
        "rules": [
            {
                "name": "deny-t3-fallback",
                "action": "Deny",
                "principal": {
                    "headers": [{"name": ":authority", "values": ["t3.jomcgi.dev"]}]
                },
            },
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
            },
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
        for claim in rule["principal"].get("jwt", {}).get("claims", [])
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
        "t3-authentik-oidc",
        "home-assistant-authentik-oidc",
        "artifact-admins",
        *(f"artifact-server-{server}" for server in SERVERS),
        "artifact-lib-no-gateway-oidc",
        "artifact-share-no-gateway-oidc",
        "notes-admins",
        *(f"notes-server-{server}" for server in SERVERS),
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
        "authorization": {
            "defaultAction": "Deny",
            "rules": gateway["authorization"]["rules"][1:],
        },
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


def test_home_assistant_admits_admins_and_household_on_the_entire_host(docs):
    route = docs["HTTPRoute", "home-assistant"]["spec"]
    assert route["hostnames"] == ["home.jomcgi.dev"]
    ((backend,),) = [rule["backendRefs"] for rule in route["rules"]]
    assert backend == {
        "group": "",
        "kind": "Service",
        "name": "home-assistant",
        "namespace": "home-assistant",
        "port": 8123,
        "weight": 1,
    }
    policy = docs["SecurityPolicy", "home-assistant-authentik-oidc"]["spec"]
    gateway = docs["SecurityPolicy", "edge-authentik-oidc"]["spec"]
    assert policy["targetRefs"] == [
        {
            "group": "gateway.networking.k8s.io",
            "kind": "HTTPRoute",
            "name": "home-assistant",
        }
    ]
    assert policy["oidc"] == gateway["oidc"]
    assert policy["authorization"]["defaultAction"] == "Deny"
    admins, household = policy["authorization"]["rules"]
    assert admins["principal"]["jwt"]["claims"] == [
        {"name": "groups", "valueType": "StringArray", "values": ["authentik Admins"]}
    ]
    assert household["action"] == "Allow"
    assert household["principal"]["jwt"]["claims"] == [
        {"name": "groups", "valueType": "StringArray", "values": ["household"]}
    ]


def test_household_is_admitted_on_home_assistant_only(docs):
    for (kind, name), doc in docs.items():
        if kind == "SecurityPolicy" and name != "home-assistant-authentik-oidc":
            assert "household" not in str(doc.get("spec", {}))


def test_artifacts_are_sandboxed_and_each_server_folder_admits_its_group(docs):
    route = docs["HTTPRoute", "artifact"]["spec"]
    assert route["hostnames"] == ["artifact.jomcgi.dev"]
    rules = {rule["name"]: rule for rule in route["rules"]}
    assert list(rules) == [f"server-{s}" for s in SERVERS] + ["admins"]
    for name, rule in rules.items():
        prefix = "/" if name == "admins" else "/" + name.removeprefix("server-") + "/"
        assert rule["matches"] == [{"path": {"type": "PathPrefix", "value": prefix}}]
        assert rule["backendRefs"] == [
            {
                "group": "",
                "kind": "Service",
                "name": "garage-web",
                "namespace": "garage",
                "port": 3902,
                "weight": 1,
            }
        ]
        assert rule["filters"] == rules["admins"]["filters"]
    filters = {f["type"]: f for f in rules["admins"]["filters"]}
    assert filters["URLRewrite"]["urlRewrite"] == {
        "hostname": "bosun-artifacts.web.garage.internal"
    }
    assert filters["RequestHeaderModifier"]["requestHeaderModifier"] == {
        "remove": ["Cookie", "Authorization"]
    }
    response = filters["ResponseHeaderModifier"]["responseHeaderModifier"]
    headers = {h["name"]: h["value"] for h in response["set"]}
    csp = dict(
        (d.split()[0], d.split()[1:])
        for d in headers.pop("Content-Security-Policy").split("; ")
    )
    assert csp["sandbox"] == ["allow-scripts", "allow-popups"]
    assert csp["default-src"] == ["'none'"]
    assert csp["connect-src"] == ["'none'"]
    assert csp["form-action"] == ["'none'"]
    assert csp["base-uri"] == ["'none'"]
    # Only script-src, style-src and font-src name a network source: the
    # libraries path on this host, nothing else.
    lib = "https://artifact.jomcgi.dev/_lib/"
    assert csp.pop("script-src") == ["'unsafe-inline'", "'unsafe-eval'", lib]
    assert csp.pop("style-src") == ["'unsafe-inline'", lib]
    assert csp.pop("font-src") == ["data:", lib]
    for values in csp.values():
        for v in values:
            assert v in {
                "allow-scripts",
                "allow-popups",
                "'none'",
                "'unsafe-inline'",
                "data:",
                "blob:",
            }, v
    assert headers == {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Resource-Policy": "same-origin",
    }
    assert response["remove"] == ["Set-Cookie", "Access-Control-Allow-Origin"]

    # One policy per rule. Each merges into the gateway policy, so it keeps
    # that sign-in and session, and replaces only the authorization.
    gateway = docs["SecurityPolicy", "edge-authentik-oidc"]["spec"]
    admins = {
        "name": "groups",
        "valueType": "StringArray",
        "values": ["authentik Admins"],
    }
    for rule in rules:
        server = rule.removeprefix("server-")
        name = "artifact-admins" if rule == "admins" else f"artifact-server-{server}"
        policy = docs["SecurityPolicy", name]["spec"]
        assert policy["targetRefs"] == [
            {
                "group": "gateway.networking.k8s.io",
                "kind": "HTTPRoute",
                "name": "artifact",
                "sectionName": rule,
            }
        ]
        assert policy["mergeType"] == "JSONMerge"
        assert "oidc" not in policy
        assert policy["jwt"] == gateway["jwt"]
        assert policy["authorization"]["defaultAction"] == "Deny"
        allowed = [
            (r["action"], r["principal"]["jwt"]["claims"])
            for r in policy["authorization"]["rules"]
        ]
        expected = [("Allow", [admins])]
        if rule != "admins":
            expected.append(("Allow", [{**admins, "values": [f"discord-{server}"]}]))
        assert allowed == expected
    # A server group is admitted on its own folder only.
    for (kind, name), doc in docs.items():
        if kind == "SecurityPolicy":
            for server in SERVERS:
                if f"discord-{server}" in str(doc["spec"]):
                    assert name in (
                        f"artifact-server-{server}",
                        f"notes-server-{server}",
                    )
    egress = docs["CiliumNetworkPolicy", "proxy"]["spec"]["egress"]
    garage = [
        r
        for r in egress
        if any(
            e.get("matchLabels", {}).get("k8s:io.kubernetes.pod.namespace") == "garage"
            for e in r.get("toEndpoints", [])
        )
    ]
    assert garage == [
        {
            "toEndpoints": [
                {
                    "matchLabels": {
                        "k8s:io.kubernetes.pod.namespace": "garage",
                        "app.kubernetes.io/name": "garage",
                    }
                }
            ],
            "toPorts": [
                {
                    "ports": [
                        {"port": "3902", "protocol": "TCP"},
                        {"port": "3900", "protocol": "TCP"},
                    ]
                }
            ],
        }
    ]


def test_notes_viewer_admits_each_server_group_on_its_folder_only(docs):
    # HOME-920: the same folders and policies as artifact.jomcgi.dev, in
    # front of the read-only viewer, with the viewer's strict CSP.
    route = docs["HTTPRoute", "notes"]["spec"]
    assert route["hostnames"] == ["notes.jomcgi.dev"]
    rules = {rule["name"]: rule for rule in route["rules"]}
    assert list(rules) == [f"server-{s}" for s in SERVERS] + ["admins"]
    for name, rule in rules.items():
        server = name.removeprefix("server-")
        matches = (
            [{"path": {"type": "PathPrefix", "value": "/"}}]
            if name == "admins"
            else [
                {"path": {"type": "PathPrefix", "value": f"/{server}/"}},
                {"path": {"type": "Exact", "value": f"/{server}"}},
            ]
        )
        assert rule["matches"] == matches
        assert rule["backendRefs"] == [
            {
                "group": "",
                "kind": "Service",
                "name": "bosun-notes-view",
                "namespace": "factory-discord",
                "port": 8080,
                "weight": 1,
            }
        ]
        assert rule["filters"] == rules["admins"]["filters"]
    filters = {f["type"]: f for f in rules["admins"]["filters"]}
    assert set(filters) == {"RequestHeaderModifier", "ResponseHeaderModifier"}
    assert filters["RequestHeaderModifier"]["requestHeaderModifier"] == {
        "remove": ["Cookie", "Authorization"]
    }
    response = filters["ResponseHeaderModifier"]["responseHeaderModifier"]
    headers = {h["name"]: h["value"] for h in response["set"]}
    csp = dict(
        (d.split()[0], d.split()[1:])
        for d in headers.pop("Content-Security-Policy").split("; ")
    )
    assert csp == {
        "default-src": ["'none'"],
        "script-src": ["'self'"],
        "style-src": ["'self'"],
        "img-src": ["'self'"],
        "font-src": ["'self'"],
        "connect-src": ["'none'"],
        "form-action": ["'self'"],
        "frame-ancestors": ["'none'"],
        "base-uri": ["'none'"],
    }
    assert headers == {
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Resource-Policy": "same-origin",
        "X-Frame-Options": "DENY",
    }
    assert response["remove"] == ["Set-Cookie", "Access-Control-Allow-Origin"]

    gateway = docs["SecurityPolicy", "edge-authentik-oidc"]["spec"]
    admins = {
        "name": "groups",
        "valueType": "StringArray",
        "values": ["authentik Admins"],
    }
    for rule in rules:
        server = rule.removeprefix("server-")
        name = "notes-admins" if rule == "admins" else f"notes-server-{server}"
        policy = docs["SecurityPolicy", name]["spec"]
        assert policy["targetRefs"] == [
            {
                "group": "gateway.networking.k8s.io",
                "kind": "HTTPRoute",
                "name": "notes",
                "sectionName": rule,
            }
        ]
        assert policy["mergeType"] == "JSONMerge"
        assert "oidc" not in policy
        assert policy["jwt"] == gateway["jwt"]
        assert policy["authorization"]["defaultAction"] == "Deny"
        allowed = [
            (r["action"], r["principal"]["jwt"]["claims"])
            for r in policy["authorization"]["rules"]
        ]
        expected = [("Allow", [admins])]
        if rule != "admins":
            expected.append(("Allow", [{**admins, "values": [f"discord-{server}"]}]))
        assert allowed == expected
    egress = docs["CiliumNetworkPolicy", "proxy"]["spec"]["egress"]
    viewer = [r for r in egress if "factory-discord" in str(r)]
    assert viewer == [
        {
            "toEndpoints": [
                {
                    "matchLabels": {
                        "k8s:io.kubernetes.pod.namespace": "factory-discord",
                        "app.kubernetes.io/name": "bosun-notes-view",
                    }
                }
            ],
            "toPorts": [{"ports": [{"port": "8080", "protocol": "TCP"}]}],
        }
    ]


def test_artifact_libraries_are_public_read_only_and_from_their_own_bucket(docs):
    route = docs["HTTPRoute", "artifact-lib"]["spec"]
    assert route["hostnames"] == ["artifact.jomcgi.dev"]
    (rule,) = route["rules"]
    assert rule["matches"] == [
        {"path": {"type": "PathPrefix", "value": "/_lib/"}, "method": "GET"}
    ]
    filters = {f["type"]: f for f in rule["filters"]}
    # The libraries bucket, never the artifacts bucket.
    assert filters["URLRewrite"]["urlRewrite"] == {
        "hostname": "bosun-libs.web.garage.internal",
        "path": {"type": "ReplacePrefixMatch", "replacePrefixMatch": "/"},
    }
    assert filters["RequestHeaderModifier"]["requestHeaderModifier"] == {
        "remove": ["Cookie", "Authorization"]
    }
    response = filters["ResponseHeaderModifier"]["responseHeaderModifier"]
    headers = {h["name"]: h["value"] for h in response["set"]}
    artifact = docs["HTTPRoute", "artifact"]["spec"]["rules"][0]["filters"]
    (artifact_headers,) = [
        f["responseHeaderModifier"]["set"]
        for f in artifact
        if f["type"] == "ResponseHeaderModifier"
    ]
    # The self-test page gets the artifact pages' sandbox.
    assert (
        headers["Content-Security-Policy"]
        == {h["name"]: h["value"] for h in artifact_headers}["Content-Security-Policy"]
    )
    assert headers["Cross-Origin-Resource-Policy"] == "cross-origin"
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert response["remove"] == ["Set-Cookie"]
    # Only this route skips sign-in, and only by name.
    policy = docs["SecurityPolicy", "artifact-lib-no-gateway-oidc"]["spec"]
    assert policy == {
        "targetRefs": [
            {
                "group": "gateway.networking.k8s.io",
                "kind": "HTTPRoute",
                "name": "artifact-lib",
            }
        ],
        "authorization": {"defaultAction": "Allow"},
    }


def test_prefix_rewrites_have_exactly_one_path_prefix_match(docs):
    # Gateway API CEL rule: a rule whose URLRewrite uses replacePrefixMatch
    # must have exactly one match, and it must be a PathPrefix. The API
    # server refuses the whole route otherwise, and Argo stops syncing edge.
    for (kind, name), doc in docs.items():
        if kind != "HTTPRoute":
            continue
        for rule in doc["spec"].get("rules", []):
            rewrites = [
                f["urlRewrite"].get("path", {})
                for f in rule.get("filters", [])
                if f["type"] == "URLRewrite"
            ]
            if any(p.get("type") == "ReplacePrefixMatch" for p in rewrites):
                matches = rule.get("matches", [])
                assert len(matches) == 1, name
                assert matches[0]["path"]["type"] == "PathPrefix", name


def test_share_links_reach_only_the_artifact_bucket_with_host_kept(docs):
    route = docs["HTTPRoute", "artifact-share"]["spec"]
    assert route["hostnames"] == ["artifact.jomcgi.dev"]
    # One rule per method: the API server admits replacePrefixMatch only
    # with exactly one PathPrefix match in its rule.
    get, head = route["rules"]
    assert get["matches"] == [
        {"path": {"type": "PathPrefix", "value": "/s"}, "method": "GET"}
    ]
    assert head["matches"] == [
        {"path": {"type": "PathPrefix", "value": "/s"}, "method": "HEAD"}
    ]
    assert (
        get["filters"] == head["filters"] and get["backendRefs"] == head["backendRefs"]
    )
    rule = get
    filters = {f["type"]: f for f in rule["filters"]}
    # Only the path prefix changes; Host stays artifact.jomcgi.dev, which
    # the presigned signature covers (discord/artifacts/share.go).
    assert filters["URLRewrite"]["urlRewrite"] == {
        "path": {
            "type": "ReplacePrefixMatch",
            "replacePrefixMatch": "/bosun-artifacts",
        }
    }
    assert filters["RequestHeaderModifier"]["requestHeaderModifier"] == {
        "remove": ["Cookie", "Authorization"]
    }
    response = filters["ResponseHeaderModifier"]["responseHeaderModifier"]
    headers = {h["name"]: h["value"] for h in response["set"]}
    artifact = {
        f["type"]: f
        for f in docs["HTTPRoute", "artifact"]["spec"]["rules"][0]["filters"]
    }
    sandbox = {
        h["name"]: h["value"]
        for h in artifact["ResponseHeaderModifier"]["responseHeaderModifier"]["set"]
    }
    assert headers == {
        **sandbox,
        "X-Robots-Tag": "noindex, nofollow",
        "Cache-Control": "private, no-store",
    }
    assert response["remove"] == ["Set-Cookie", "Access-Control-Allow-Origin"]
    assert rule["backendRefs"] == [
        {
            "group": "",
            "kind": "Service",
            "name": "garage",
            "namespace": "garage",
            "port": 3900,
            "weight": 1,
        }
    ]
    # No sign-in: the signature is the credential, and Garage checks it.
    assert docs["SecurityPolicy", "artifact-share-no-gateway-oidc"]["spec"] == {
        "targetRefs": [
            {
                "group": "gateway.networking.k8s.io",
                "kind": "HTTPRoute",
                "name": "artifact-share",
            }
        ],
        "authorization": {"defaultAction": "Allow"},
    }


def test_t3_requires_admin_group_on_the_entire_host(docs):
    route = docs["HTTPRoute", "t3"]["spec"]
    assert route["hostnames"] == ["t3.jomcgi.dev"]
    assert route["rules"] == [
        {
            "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
            "timeouts": {"request": "0s", "backendRequest": "0s"},
            "backendRefs": [
                {
                    "group": "",
                    "kind": "Service",
                    "name": "t3-relay",
                    "namespace": "t3",
                    "port": 8080,
                    "weight": 1,
                }
            ],
        }
    ]
    policy = docs["SecurityPolicy", "t3-authentik-oidc"]["spec"]
    gateway = docs["SecurityPolicy", "edge-authentik-oidc"]["spec"]
    assert policy["targetRefs"] == [
        {"group": "gateway.networking.k8s.io", "kind": "HTTPRoute", "name": "t3"}
    ]
    assert policy["oidc"] == gateway["oidc"]
    assert policy["jwt"] == gateway["jwt"]
    assert policy["authorization"]["defaultAction"] == "Deny"
    (rule,) = policy["authorization"]["rules"]
    assert rule["action"] == "Allow"
    assert rule["principal"]["jwt"]["claims"] == [
        {"name": "groups", "valueType": "StringArray", "values": ["authentik Admins"]}
    ]


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
                                        "factory-kube-sessions",
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
        "notes.jomcgi.dev",
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
        metadata = doc["metadata"]
        options = metadata["annotations"]["argocd.argoproj.io/sync-options"]
        expected = (
            "Prune=false,Delete=false"
            if metadata["name"].startswith("factory-t3-")
            else "Delete=false"
        )
        assert options == expected
        if doc["kind"] == "ValidatingAdmissionPolicy":
            assert doc["spec"]["failurePolicy"] == "Fail"
        else:
            assert doc["spec"]["validationActions"] == ["Deny"]
    tunnel = Path(
        os.environ.get("EDGE_TUNNEL", platform / "edge-tunnel/resources/route.yaml")
    )
    route = yaml.safe_load(tunnel.read_text())
    assert route["metadata"]["namespace"] == "edge"
    assert any(
        rule["message"] == "Only edge/t3 and the edge tunnel may route t3.jomcgi.dev."
        for rule in validations
    )
    assert {
        "factory-agents.jomcgi.dev",
        "factory-kube.jomcgi.dev",
        "factory-explore-api.jomcgi.dev",
        "notes.jomcgi.dev",
    } <= set(route["spec"]["hostnames"])
    assert "t3.jomcgi.dev" in route["spec"]["hostnames"]
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
