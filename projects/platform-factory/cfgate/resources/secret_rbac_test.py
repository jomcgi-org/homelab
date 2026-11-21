"""Check the Secret grants bound to the deployed cfgate controller."""

import os
import subprocess
from collections import defaultdict
from pathlib import Path

import yaml


def test_controller_secret_grants():
    render = subprocess.check_output(
        [os.environ["KUSTOMIZE_BIN"], "build", str(Path(__file__).resolve().parent)]
    )
    docs = list(yaml.safe_load_all(render))
    (deployment,) = [d for d in docs if d["kind"] == "Deployment"]
    namespace = deployment["metadata"]["namespace"]
    account = deployment["spec"]["template"]["spec"]["serviceAccountName"]
    assert namespace == "cfgate-system"
    identities = {
        ("User", f"system:serviceaccount:{namespace}:{account}"),
        ("Group", "system:authenticated"),
        ("Group", "system:serviceaccounts"),
        ("Group", f"system:serviceaccounts:{namespace}"),
    }
    roles = {
        (d["kind"], d["metadata"].get("namespace"), d["metadata"]["name"]): d
        for d in docs
        if d["kind"] in {"Role", "ClusterRole"}
    }
    grants = defaultdict(set)
    unrestricted = defaultdict(set)
    for binding in docs:
        if binding["kind"] not in {"RoleBinding", "ClusterRoleBinding"}:
            continue
        scope = binding["metadata"].get("namespace")
        if not any(
            (s["kind"], s["name"]) in identities
            or (
                s["kind"] == "ServiceAccount"
                and s["name"] == account
                and s.get("namespace", scope) == namespace
            )
            for s in binding.get("subjects", [])
        ):
            continue
        ref = binding["roleRef"]
        role = roles[ref["kind"], scope if ref["kind"] == "Role" else None, ref["name"]]
        for rule in role["rules"]:
            if {"", "*"} & set(rule.get("apiGroups", [])) and {"secrets", "*"} & set(
                rule.get("resources", [])
            ):
                grants[scope].update(rule["verbs"])
                if not rule.get("resourceNames"):
                    unrestricted[scope].update(rule["verbs"])

    expected = {
        None: {"list", "watch"},
        namespace: {"get", "list", "watch", "create", "patch", "update", "delete"},
    }
    assert grants == expected
    assert unrestricted == expected
