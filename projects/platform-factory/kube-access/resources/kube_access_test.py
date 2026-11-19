"""Check factory kubectl and explorer access."""

from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).parent
DOCS = [
    d
    for name in yaml.safe_load((HERE / "kustomization.yaml").read_text())["resources"]
    for d in yaml.safe_load_all((HERE / name).read_text())
    if d
]
VIEWERS = "authentik:factory-kube-viewers"
EXPLORERS = "explore:explorers"
ADMINS = "authentik:authentik Admins"
# Reviewed in #8222: no private data in their objects or logs. Adding one is
# Joe's decision; agent, ticket, chat, memory and sign-in namespaces stay out.
VIEWER_NAMESPACES = {
    "argocd",
    "kargo",
    "cert-manager",
    "cnpg-system",
    "garage",
    "kube-system",
    "jomcgi-site-dev",
    "jomcgi-site-prod",
}
READ_VERBS = {"get", "list", "watch"}
FORBIDDEN = {
    "secrets",
    "configmaps",
    "pods/exec",
    "pods/attach",
    "pods/portforward",
    "serviceaccounts/token",
    "roles",
    "rolebindings",
    "clusterroles",
    "clusterrolebindings",
}


def by_kind(kind):
    return [d for d in DOCS if d["kind"] == kind]


def test_only_admins_hold_cluster_admin():
    (binding,) = [
        d
        for d in by_kind("ClusterRoleBinding")
        if d["roleRef"]["name"] in {"cluster-admin", "admin", "edit"}
    ]
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": "cluster-admin",
    }
    assert binding["subjects"] == [
        {"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": group}
        for group in (ADMINS, "authentik:factory-operators")
    ]
    for d in by_kind("RoleBinding"):
        assert d["roleRef"]["name"] not in {"cluster-admin", "admin", "edit"}


def test_every_subject_is_an_access_group():
    for d in by_kind("ClusterRoleBinding") + by_kind("RoleBinding"):
        for subject in d["subjects"]:
            assert subject["kind"] == "Group"
            assert subject["name"] in {
                ADMINS,
                "authentik:factory-operators",
                VIEWERS,
                EXPLORERS,
            }


def test_viewer_roles_only_read():
    roles = {d["metadata"]["name"]: d for d in by_kind("ClusterRole")}
    assert set(roles) == {
        "factory-viewer",
        "factory-viewer-cluster",
        "factory-explorer",
        "factory-explorer-cluster",
    }
    for role in roles.values():
        for rule in role["rules"]:
            assert set(rule["verbs"]) <= READ_VERBS, rule
            assert "*" not in rule["apiGroups"]
            for resource in rule["resources"]:
                if role["metadata"]["name"].startswith("factory-explorer"):
                    assert resource != "pods/log"
                assert "*" not in resource
                assert resource not in FORBIDDEN, resource
                assert not resource.endswith("/proxy"), resource
            assert "resourceNames" not in rule
        assert "aggregationRule" not in role


@pytest.mark.parametrize(
    ("group", "role"), [(VIEWERS, "factory-viewer"), (EXPLORERS, "factory-explorer")]
)
def test_readers_cover_only_the_reviewed_namespaces(group, role):
    bindings = [
        d
        for d in by_kind("RoleBinding")
        if any(s["name"] == group for s in d["subjects"])
    ]
    assert {d["metadata"]["namespace"] for d in bindings} == VIEWER_NAMESPACES
    assert len(bindings) == len(VIEWER_NAMESPACES)
    for d in bindings:
        assert d["roleRef"]["name"] == role
    (cluster,) = [
        d
        for d in by_kind("ClusterRoleBinding")
        if any(s["name"] == group for s in d["subjects"])
    ]
    assert cluster["roleRef"]["name"] == role + "-cluster"


def test_authentication_config():
    config = HERE.parents[2] / "factory-cluster/nodes/authentication-config.yaml"
    assert yaml.safe_load(config.read_text())["jwt"] == [
        {
            "issuer": {
                "url": "https://factory-auth.jomcgi.dev/application/o/factory-kubectl/",
                "audiences": ["factory-kubectl"],
            },
            "claimMappings": {
                "username": {"claim": "preferred_username", "prefix": "authentik:"},
                "groups": {"claim": "groups", "prefix": "authentik:"},
                "uid": {"claim": "sub"},
            },
        },
        {
            "issuer": {
                "url": "https://factory-auth.jomcgi.dev/application/o/factory-explore/",
                "audiences": ["factory-explore"],
            },
            "claimMappings": {
                "username": {"claim": "preferred_username", "prefix": "explore:"},
                "groups": {"expression": f"['{EXPLORERS}']"},
                "uid": {"claim": "sub"},
            },
            "claimValidationRules": [
                {"expression": "double(claims.exp) - double(claims.iat) <= 600.0"}
            ],
        },
    ]


def test_explorer_priority_level_limits_concurrency():
    (level,) = by_kind("PriorityLevelConfiguration")
    assert level["apiVersion"] == "flowcontrol.apiserver.k8s.io/v1"
    assert level["metadata"]["name"] == "factory-explorers"
    assert level["spec"]["type"] == "Limited"
    assert level["spec"]["limited"] == {
        "nominalConcurrencyShares": 5,
        "borrowingLimitPercent": 0,
        "limitResponse": {
            "type": "Queue",
            "queuing": {"queues": 16, "handSize": 4, "queueLengthLimit": 20},
        },
    }


def test_all_explorer_requests_use_the_explorer_priority_level():
    (schema,) = by_kind("FlowSchema")
    assert schema["apiVersion"] == "flowcontrol.apiserver.k8s.io/v1"
    assert schema["metadata"]["name"] == "factory-explorers"
    spec = schema["spec"]
    assert spec["priorityLevelConfiguration"] == {"name": "factory-explorers"}
    assert spec["matchingPrecedence"] == 1000
    assert spec["distinguisherMethod"] == {"type": "ByUser"}
    assert spec["rules"] == [
        {
            "subjects": [{"kind": "Group", "group": {"name": EXPLORERS}}],
            "resourceRules": [
                {
                    "verbs": ["*"],
                    "apiGroups": ["*"],
                    "resources": ["*"],
                    "namespaces": ["*"],
                    "clusterScope": True,
                }
            ],
            "nonResourceRules": [{"verbs": ["*"], "nonResourceURLs": ["*"]}],
        }
    ]
