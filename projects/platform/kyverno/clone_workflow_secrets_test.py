"""Rendered production source, target, rotation and exact membership contract."""

import copy
import os
from pathlib import Path
import subprocess

import pytest
import yaml


EXPECTED = {
    "clone-to-monolith-workflows": "monolith-pg-app",
    "clone-clickhouse-to-monolith-workflows": "monolith-clickhouse",
    "clone-secrets-to-monolith-workflows": "monolith-secrets",
    "clone-chat-secrets-to-monolith-workflows": "monolith-chat-secrets",
    "clone-openrouter-to-monolith-workflows": "goosecracker",
    "clone-meta-spark-to-monolith-workflows": "monolith-meta-spark",
    "clone-pg-dump-reader-to-monolith-workflows": "monolith-pg-dump-reader",
    "clone-r2-s3-to-monolith-workflows": "monolith-r2-s3",
}


@pytest.fixture(scope="module")
def objects():
    chart = Path(__file__).resolve().parent
    result = subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "kyverno",
            str(chart),
            "--namespace",
            "kyverno",
            "--show-only",
            "templates/clone-monolith-pg-secret-policy.yaml",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _assert_production_policy(policy):
    assert policy["metadata"]["name"] == "clone-production-monolith-workflow-secrets"
    assert policy["spec"]["background"] is True
    rules = policy["spec"]["rules"]
    assert len(rules) == len(EXPECTED)
    assert {rule["name"] for rule in rules} == set(EXPECTED)
    for rule in rules:
        secret = EXPECTED[rule["name"]]
        assert rule["skipBackgroundRequests"] is True
        assert rule["match"] == {
            "any": [
                {
                    "resources": {
                        "kinds": ["Secret"],
                        "namespaces": ["monolith"],
                        "names": [secret],
                    }
                }
            ]
        }
        assert rule["generate"] == {
            "apiVersion": "v1",
            "kind": "Secret",
            "name": secret,
            "namespace": "monolith-workflows",
            "synchronize": True,
            "generateExisting": True,
            "clone": {"namespace": "monolith", "name": secret},
        }


def test_exact_production_rules_and_source_triggered_rotation(objects):
    policies = [doc for doc in objects if doc["kind"] == "ClusterPolicy"]
    assert len(policies) == 1
    _assert_production_policy(policies[0])


def test_controllers_keep_their_secret_access(objects):
    roles = {
        doc["metadata"]["name"]: doc for doc in objects if doc["kind"] == "ClusterRole"
    }
    assert set(roles) == {
        "kyverno:clone-monolith-pg-secret-background",
        "kyverno:clone-monolith-pg-secret-admission",
    }
    for controller, verbs in (
        ("background", ["get", "list", "watch", "create", "update", "delete"]),
        ("admission", ["get", "list", "watch"]),
    ):
        role = roles[f"kyverno:clone-monolith-pg-secret-{controller}"]
        assert (
            role["metadata"]["labels"][
                f"rbac.kyverno.io/aggregate-to-{controller}-controller"
            ]
            == "true"
        )
        assert role["rules"] == [
            {"apiGroups": [""], "resources": ["secrets"], "verbs": verbs}
        ]


@pytest.mark.parametrize(
    "broken", ["empty", "source", "target", "rotation", "backfill"]
)
def test_contract_rejects_collateral_removal_or_weakened_generation(objects, broken):
    policy = copy.deepcopy(
        next(doc for doc in objects if doc["kind"] == "ClusterPolicy")
    )
    if broken == "empty":
        policy["spec"]["rules"] = []
    else:
        rule = policy["spec"]["rules"][0]
        if broken == "source":
            rule["match"]["any"][0]["resources"]["namespaces"] = ["other"]
        elif broken == "target":
            rule["generate"]["namespace"] = "other"
        elif broken == "rotation":
            rule["generate"]["synchronize"] = False
        else:
            rule["generate"]["generateExisting"] = False
    with pytest.raises(AssertionError):
        _assert_production_policy(policy)
