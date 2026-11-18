"""kubectl RBAC for Authentik groups: admins get cluster-admin, everyone else
reads only, and only in namespaces whose objects and logs hold no private data.

The API server prefixes every Authentik user and group with "authentik:"
(projects/factory-cluster/nodes/authentication-config.yaml)."""

from pathlib import Path

import yaml

HERE = Path(__file__).parent
DOCS = [
    d
    for name in ("admins.yaml", "viewers.yaml")
    for d in yaml.safe_load_all((HERE / name).read_text())
    if d
]
VIEWERS = "authentik:factory-kube-viewers"
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
        {"apiGroup": "rbac.authorization.k8s.io", "kind": "Group", "name": ADMINS}
    ]
    for d in by_kind("RoleBinding"):
        assert d["roleRef"]["name"] not in {"cluster-admin", "admin", "edit"}


def test_every_subject_is_a_prefixed_authentik_group():
    for d in by_kind("ClusterRoleBinding") + by_kind("RoleBinding"):
        for subject in d["subjects"]:
            assert subject["kind"] == "Group"
            assert subject["name"].startswith("authentik:")


def test_viewer_roles_only_read():
    roles = {d["metadata"]["name"]: d for d in by_kind("ClusterRole")}
    assert set(roles) == {"factory-viewer", "factory-viewer-cluster"}
    for role in roles.values():
        for rule in role["rules"]:
            assert set(rule["verbs"]) <= READ_VERBS, rule
            assert "*" not in rule["apiGroups"]
            for resource in rule["resources"]:
                assert "*" not in resource
                assert resource not in FORBIDDEN, resource
                assert not resource.endswith("/proxy"), resource
            assert "resourceNames" not in rule
        assert "aggregationRule" not in role


def test_viewers_read_only_the_reviewed_namespaces():
    bindings = [
        d
        for d in by_kind("RoleBinding")
        if any(s["name"] == VIEWERS for s in d["subjects"])
    ]
    assert {d["metadata"]["namespace"] for d in bindings} == VIEWER_NAMESPACES
    assert len(bindings) == len(VIEWER_NAMESPACES)
    for d in bindings:
        assert d["roleRef"]["name"] == "factory-viewer"
    (cluster,) = [
        d
        for d in by_kind("ClusterRoleBinding")
        if any(s["name"] == VIEWERS for s in d["subjects"])
    ]
    assert cluster["roleRef"]["name"] == "factory-viewer-cluster"
