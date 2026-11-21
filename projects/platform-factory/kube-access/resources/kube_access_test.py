"""Check factory kubectl and explorer access."""

import json
import os
import runpy
import subprocess
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).parent
NODE_FILES = HERE.parents[2] / "factory-cluster/nodes"
TOKEN_HELPER = str(NODE_FILES / "factory-session-token.py")
DOCS = [
    d
    for name in yaml.safe_load((HERE / "kustomization.yaml").read_text())["resources"]
    for d in yaml.safe_load_all((HERE / name).read_text())
    if d
]
VIEWERS = "authentik:factory-kube-viewers"
EXPLORERS = "explore:explorers"
SESSIONS = "authentik:factory-kube-sessions"
NODE_SESSION = {
    "kind": "ServiceAccount",
    "name": "factory-node1-sessions",
    "namespace": "kube-system",
}
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
# Core subresources that serve GET, from the factory cluster API discovery
# (/api/v1, Kubernetes v1.37). kubectl api-resources lists no subresources.
CORE_GET_SUBRESOURCES = {
    "namespaces/status",
    "nodes/proxy",
    "nodes/status",
    "persistentvolumeclaims/status",
    "persistentvolumes/status",
    "pods/attach",
    "pods/ephemeralcontainers",
    "pods/exec",
    "pods/log",
    "pods/portforward",
    "pods/proxy",
    "pods/resize",
    "pods/status",
    "replicationcontrollers/scale",
    "replicationcontrollers/status",
    "resourcequotas/status",
    "services/proxy",
    "services/status",
}
# A GET on these opens a command stream or a proxy to the kubelet or a
# workload: a write, so sessions never get them cluster-wide.
STREAM_SUBRESOURCES = {
    "nodes/proxy",
    "pods/attach",
    "pods/exec",
    "pods/portforward",
    "pods/proxy",
    "services/proxy",
}
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


@pytest.mark.parametrize("undeclared_allowed", [False, True])
def test_session_verifier_checks_undeclared_group(tmp_path, undeclared_allowed):
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(
        """#!/bin/bash
case "$1" in
    get)
        [[ "$2 $3" == "clusterrole factory-sessions" ]] || exit 2
        printf '%s\\n' apps
        ;;
    api-resources)
        printf '%s\\n' pods secrets deployments.apps policyreports.wgpolicyk8s.io
        ;;
    auth)
        [[ "$2" == "can-i" ]] || exit 2
        case " $* " in
            *" policyreports.wgpolicyk8s.io "*)
                if [[ "$ALLOW_POLICY_REPORTS" == 1 ]]; then
                    echo yes
                    exit 0
                fi
                ;;
            *" --quiet "*) exit 0 ;;
        esac
        echo no
        exit 1
        ;;
    *) exit 2 ;;
esac
"""
    )
    kubectl.chmod(0o700)
    result = subprocess.run(
        ["bash", str(HERE.parent / "check-session-access.sh")],
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "ALLOW_POLICY_REPORTS": str(int(undeclared_allowed)),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    if undeclared_allowed:
        assert result.returncode != 0
        assert (
            "Unexpected cluster-wide get on policyreports.wgpolicyk8s.io" in result.stderr
        )
    else:
        assert result.returncode == 0, result.stderr


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


def test_every_subject_is_an_access_group_or_the_node_session():
    assert by_kind("ServiceAccount") == [
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {
                "name": NODE_SESSION["name"],
                "namespace": NODE_SESSION["namespace"],
            },
            "automountServiceAccountToken": False,
        }
    ]
    for d in by_kind("ClusterRoleBinding") + by_kind("RoleBinding"):
        for subject in d["subjects"]:
            if d["roleRef"]["name"] == "factory-sessions" and subject == NODE_SESSION:
                continue
            assert subject["kind"] == "Group"
            assert subject["name"] in {
                ADMINS,
                "authentik:factory-operators",
                VIEWERS,
                EXPLORERS,
                SESSIONS,
            }


def test_viewer_roles_only_read():
    roles = {d["metadata"]["name"]: d for d in by_kind("ClusterRole")}
    roles.pop("factory-sessions")
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
    config = NODE_FILES / "authentication-config.yaml"
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


def test_session_permissions():
    for kind in ("ClusterRole", "Role"):
        (binding,) = [
            b
            for b in by_kind(kind + "Binding")
            if any(s["name"] == SESSIONS for s in b["subjects"])
        ]
        assert binding["subjects"] == [
            {
                "kind": "Group",
                "apiGroup": "rbac.authorization.k8s.io",
                "name": SESSIONS,
            },
            NODE_SESSION,
        ]
        namespace = "factory" if kind == "Role" else None
        assert binding["metadata"].get("namespace") == namespace
        assert binding["roleRef"] == {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": kind,
            "name": "factory-sessions",
        }
        (role,) = [
            r
            for r in by_kind(kind)
            if r["metadata"]["name"] == "factory-sessions"
            and r["metadata"].get("namespace") == namespace
        ]
        if kind == "Role":
            assert role["rules"] == [
                {
                    "apiGroups": [""],
                    "resources": ["pods/portforward"],
                    "resourceNames": ["factory-itsaplan-session-forward-0"],
                    "verbs": ["get", "create"],
                },
                {
                    "apiGroups": [""],
                    "resources": ["secrets"],
                    "verbs": ["get"],
                    "resourceNames": ["factory-itsaplan-joe-sessions"],
                },
            ]
        else:
            assert "aggregationRule" not in role
            core, extensions = role["rules"]
            assert core["apiGroups"] == [""]
            assert set(core["resources"]) == set(
                "componentstatuses configmaps endpoints events limitranges namespaces namespaces/status nodes nodes/status persistentvolumeclaims persistentvolumeclaims/status persistentvolumes persistentvolumes/status pods pods/ephemeralcontainers pods/log pods/resize pods/status podtemplates replicationcontrollers replicationcontrollers/scale replicationcontrollers/status resourcequotas resourcequotas/status serviceaccounts services services/status".split()
            )
            # Every readable core subresource, except the stream and proxy
            # ones, which act as writes.
            subresources = {r for r in core["resources"] if "/" in r}
            assert subresources == CORE_GET_SUBRESOURCES - STREAM_SUBRESOURCES
            assert {"", "*"}.isdisjoint(extensions["apiGroups"])
            assert set(extensions["apiGroups"]) == {
                "acme.cert-manager.io",
                "admissionregistration.k8s.io",
                "api.kagent.dev",
                "apiextensions.k8s.io",
                "apiregistration.k8s.io",
                "apps",
                "argoproj.io",
                "ate.dev",
                "authentication.k8s.io",
                "authorization.k8s.io",
                "autoscaling",
                "batch",
                "cert-manager.io",
                "certificates.k8s.io",
                "cfgate.io",
                "cilium.io",
                "coordination.k8s.io",
                "deuxfleurs.fr",
                "discovery.k8s.io",
                "embervm.dev",
                "events.k8s.io",
                "external-secrets.io",
                "flowcontrol.apiserver.k8s.io",
                "gateway.envoyproxy.io",
                "gateway.networking.k8s.io",
                "gateway.networking.x-k8s.io",
                "generators.external-secrets.io",
                "helm.cattle.io",
                "internal.apiserver.k8s.io",
                "k3s.cattle.io",
                "kargo.akuity.io",
                "metrics.k8s.io",
                "monitoring.coreos.com",
                "networking.k8s.io",
                "node.k8s.io",
                "oci-model-cache.jomcgi.dev",
                "onepassword.com",
                "policy",
                "postgresql.cnpg.io",
                "rbac.authorization.k8s.io",
                "resource.k8s.io",
                "scheduling.k8s.io",
                "storage.k8s.io",
                "storagemigration.k8s.io",
            }
            assert extensions["resources"] == ["*"]
            for rule in role["rules"]:
                assert set(rule) == {"apiGroups", "resources", "verbs"}
                assert set(rule["verbs"]) == READ_VERBS


@pytest.mark.parametrize(
    "verb,resource,name,namespace,allowed",
    [
        (
            "create",
            "pods/portforward",
            "factory-itsaplan-session-forward-0",
            "factory",
            True,
        ),
        (
            "create",
            "pods/portforward",
            "factory-itsaplan-web-example",
            "factory",
            False,
        ),
        (
            "create",
            "pods/portforward",
            "factory-itsaplan-session-forward-0",
            "default",
            False,
        ),
        ("get", "secrets", "factory-itsaplan-joe-sessions", "factory", True),
        ("get", "secrets", "factory-itsaplan-joe-sessions", "default", False),
        ("get", "secrets", "factory-itsaplan-admin", "factory", False),
        ("update", "secrets", "factory-itsaplan-joe-sessions", "factory", False),
        ("patch", "secrets", "factory-itsaplan-joe-sessions", "factory", False),
        ("delete", "secrets", "factory-itsaplan-joe-sessions", "factory", False),
        ("list", "secrets", None, "factory", False),
        ("create", "serviceaccounts/token", NODE_SESSION["name"], "kube-system", False),
        ("watch", "secrets", None, "factory", False),
        ("create", "pods/exec", "factory-db-1", "factory", False),
        (
            "get",
            "pods/portforward",
            "factory-itsaplan-session-forward-0",
            "factory",
            True,
        ),
        ("get", "pods/portforward", "factory-itsaplan-web-example", "factory", False),
        ("get", "pods/log", "example", "default", True),
        ("get", "pods/ephemeralcontainers", "example", "default", True),
        ("get", "nodes/status", "node-1", None, True),
        ("get", "replicationcontrollers/scale", "example", "default", True),
        ("get", "nodes/proxy", "node-1", None, False),
        ("get", "pods/exec", "factory-db-1", "factory", False),
        ("get", "pods/attach", "factory-db-1", "factory", False),
        ("get", "pods/proxy", "factory-db-1", "factory", False),
        ("get", "services/proxy", "example", "factory", False),
        ("patch", "pods", "example", "factory", False),
        ("watch", "pods", None, "default", True),
    ],
)
@pytest.mark.parametrize("subject", [SESSIONS, NODE_SESSION["name"]])
def test_session_core_requests(verb, resource, name, namespace, allowed, subject):
    rules = []
    for binding in by_kind("RoleBinding") + by_kind("ClusterRoleBinding"):
        if binding["metadata"].get("namespace", namespace) != namespace:
            continue
        if not any(s["name"] == subject for s in binding["subjects"]):
            continue
        ref = binding["roleRef"]
        for role in by_kind(ref["kind"]):
            if (
                role["metadata"]["name"] == ref["name"]
                and role["metadata"].get("namespace", namespace) == namespace
            ):
                rules.extend(role["rules"])
    assert (
        any(
            {"", "*"}.intersection(rule["apiGroups"])
            and {verb, "*"}.intersection(rule["verbs"])
            and {resource, "*"}.intersection(rule["resources"])
            and ("resourceNames" not in rule or name in rule["resourceNames"])
            for rule in rules
        )
        == allowed
    )


def test_node_session_kubeconfig_and_exec_credential(monkeypatch, capsys):
    config = yaml.safe_load(
        (NODE_FILES / "factory-session-kubeconfig.yaml").read_text()
    )
    assert config["clusters"] == [
        {
            "name": "factory-local",
            "cluster": {
                "server": "https://127.0.0.1:6443",
                "certificate-authority": "/etc/factory/k3s-server-ca.crt",
            },
        }
    ]
    assert config["current-context"] == "factory"
    assert config["contexts"] == [
        {
            "name": "factory",
            "context": {"cluster": "factory-local", "user": NODE_SESSION["name"]},
        }
    ]
    version = "client.authentication.k8s.io/v1"
    assert config["users"] == [
        {
            "name": NODE_SESSION["name"],
            "user": {
                "exec": {
                    "apiVersion": version,
                    "command": "/usr/bin/sudo",
                    "args": ["-n", "--", "/usr/local/sbin/factory-session-token"],
                    "interactiveMode": "Never",
                }
            },
        }
    ]
    credential = {
        "token": "fixture-token",
        "expirationTimestamp": "2026-11-21T09:10:00Z",
    }

    def request_token(command, **options):
        endpoint = (
            f"/api/v1/namespaces/{NODE_SESSION['namespace']}/"
            f"serviceaccounts/{NODE_SESSION['name']}/token"
        )
        assert command == [
            "/usr/local/bin/k3s",
            "kubectl",
            "--kubeconfig=/etc/rancher/k3s/k3s.yaml",
            "--server=https://127.0.0.1:6443",
            "--request-timeout=10s",
            "create",
            f"--raw={endpoint}",
            "-f",
            "-",
        ]
        assert json.loads(options["input"]) == {
            "apiVersion": "authentication.k8s.io/v1",
            "kind": "TokenRequest",
            "spec": {"expirationSeconds": 600},
        }
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"status": credential, "kind": "TokenRequest"})
        )

    monkeypatch.setattr("socket.gethostname", lambda: "node-1")
    monkeypatch.setattr("sys.argv", [TOKEN_HELPER])
    monkeypatch.setattr(subprocess, "run", request_token)
    runpy.run_path(TOKEN_HELPER)
    assert json.loads(capsys.readouterr().out) == {
        "apiVersion": version,
        "kind": "ExecCredential",
        "status": credential,
    }


@pytest.mark.parametrize("node,args", [("node-2", []), ("node-1", ["extra"])])
def test_node_session_helper_rejects_other_nodes_and_arguments(monkeypatch, node, args):
    monkeypatch.setattr("socket.gethostname", lambda: node)
    monkeypatch.setattr("sys.argv", [TOKEN_HELPER, *args])
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_options: pytest.fail("unexpected token request"),
    )
    with pytest.raises(SystemExit, match="available only on node-1"):
        runpy.run_path(TOKEN_HELPER)
