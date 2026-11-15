"""Render the credential boundary without fetching the upstream operator chart."""

import os
from pathlib import Path
import re
import subprocess

import pytest
import yaml


@pytest.fixture(scope="module")
def docs():
    result = subprocess.run(
        [
            os.environ["HELM_BIN"],
            "template",
            "external-secrets",
            str(Path(__file__).parent),
            "--namespace",
            "external-secrets",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in yaml.safe_load_all(result.stdout)
        if doc
    }


def test_admission_policies_fail_closed_and_are_kept(docs):
    expected = {
        "factory-credentials-no-pods": {("", "pods")},
        "factory-credentials-generators": {
            ("generators.external-secrets.io", "githubaccesstokens"),
            ("generators.external-secrets.io", "webhooks"),
        },
        "factory-credentials-github-scope": {
            ("generators.external-secrets.io", "githubaccesstokens"),
        },
        "factory-credentials-no-cluster-or-push": {
            ("generators.external-secrets.io", "clustergenerators"),
            ("external-secrets.io", "clustersecretstores"),
            ("external-secrets.io", "clusterexternalsecrets"),
            ("external-secrets.io", "pushsecrets"),
            ("external-secrets.io", "clusterpushsecrets"),
        },
        "factory-credentials-secret-readers": {("rbac.authorization.k8s.io", "roles")},
    }
    assert {name for kind, name in docs if kind == "ValidatingAdmissionPolicy"} == set(
        expected
    )
    assert {
        name for kind, name in docs if kind == "ValidatingAdmissionPolicyBinding"
    } == set(expected)
    for name, resources in expected.items():
        policy = docs["ValidatingAdmissionPolicy", name]
        binding = docs["ValidatingAdmissionPolicyBinding", name]
        for doc in (policy, binding):
            annotations = doc["metadata"]["annotations"]
            assert "Delete=false" in annotations["argocd.argoproj.io/sync-options"]
            assert int(annotations["argocd.argoproj.io/sync-wave"]) < 0
        spec = policy["spec"]
        assert spec["failurePolicy"] == "Fail"
        assert spec["validations"]
        # No selector or match condition may exclude factory-credentials or
        # permit a namespace-label change to evade a guard.
        assert set(spec) == {"failurePolicy", "matchConstraints", "validations"}
        assert set(spec["matchConstraints"]) == {"resourceRules"}
        rules = spec["matchConstraints"]["resourceRules"]
        assert {
            (group, resource)
            for rule in rules
            for group in rule["apiGroups"]
            for resource in rule["resources"]
        } == resources
        for rule in rules:
            assert set(rule["operations"]) == {"CREATE", "UPDATE"}
            versions = (
                ["v1"] if name.endswith(("no-pods", "secret-readers")) else ["*"]
            )
            assert rule["apiVersions"] == versions
        assert binding["spec"] == {"policyName": name, "validationActions": ["Deny"]}


def test_namespace_has_no_workloads_or_minting(docs):
    namespace = docs["Namespace", "factory-credentials"]
    assert "Prune=false" in namespace["metadata"]["annotations"][
        "argocd.argoproj.io/sync-options"
    ]
    assert {kind for kind, _ in docs} == {
        "Namespace",
        "CiliumNetworkPolicy",
        "ValidatingAdmissionPolicy",
        "ValidatingAdmissionPolicyBinding",
    }
    assert docs["ValidatingAdmissionPolicy", "factory-credentials-no-pods"]["spec"][
        "validations"
    ][0]["expression"] == "request.namespace != 'factory-credentials'"
    assert docs["ValidatingAdmissionPolicy", "factory-credentials-generators"]["spec"][
        "validations"
    ][0]["expression"] == "request.namespace == 'factory-credentials'"
    assert docs["ValidatingAdmissionPolicy", "factory-credentials-no-cluster-or-push"][
        "spec"
    ]["validations"][0]["expression"] == "false"


def test_tokens_require_explicit_scope_and_roles_only_named_get(docs):
    github = docs["ValidatingAdmissionPolicy", "factory-credentials-github-scope"]
    expressions = [
        " ".join(v["expression"].split()) for v in github["spec"]["validations"]
    ]
    assert expressions == [
        "has(object.spec) && has(object.spec.repositories) && "
        "size(object.spec.repositories) > 0 && "
        "object.spec.repositories.all(r, size(r) > 0)",
        "has(object.spec) && has(object.spec.permissions) && "
        "size(object.spec.permissions) > 0 && "
        "object.spec.permissions.all(p, "
        "size(p) > 0 && size(object.spec.permissions[p]) > 0)",
    ]
    reader = docs["ValidatingAdmissionPolicy", "factory-credentials-secret-readers"]
    expression = " ".join(reader["spec"]["validations"][0]["expression"].split())
    assert expression == (
        "request.namespace != 'factory-credentials' || "
        "!has(object.rules) || object.rules.all(r, "
        "!(has(r.apiGroups) && r.apiGroups.exists(g, g == '' || g == '*') && "
        "has(r.resources) && r.resources.exists(s, "
        "s == '*' || s == 'secrets' || s.startsWith('secrets/'))) || "
        "(has(r.resourceNames) && size(r.resourceNames) > 0 && "
        "r.resourceNames.all(n, size(n) > 0) && "
        "has(r.verbs) && size(r.verbs) > 0 && r.verbs.all(v, v == 'get')))"
    )


def test_application_pins_upstream_and_installs_guards():
    root = Path(__file__).resolve().parent.parent
    application = yaml.safe_load(
        Path(os.environ.get("ESO_APPLICATION", root / "application.yaml")).read_text()
    )
    sources = application["spec"]["sources"]
    upstream = next(
        source for source in sources if source.get("chart") == "external-secrets"
    )
    assert upstream["repoURL"] == "https://charts.external-secrets.io"
    assert re.fullmatch(r"\d+\.\d+\.\d+", upstream["targetRevision"])
    assert upstream["helm"]["releaseName"] == "external-secrets"
    assert upstream["helm"]["valueFiles"] == [
        "$values/projects/platform-factory/external-secrets/values.yaml"
    ]
    assert upstream["helm"]["ignoreMissingValueFiles"] is False
    assert any(source.get("ref") == "values" for source in sources)
    assert any(
        source.get("path") == "projects/platform-factory/external-secrets/chart"
        for source in sources
    )
    assert application["spec"]["destination"]["namespace"] == "external-secrets"
    assert application["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "1"
    assert "ServerSideApply=true" in application["spec"]["syncPolicy"]["syncOptions"]


def test_upstream_values_disable_unused_controllers_and_budget_every_component():
    root = Path(__file__).resolve().parent.parent
    values = yaml.safe_load(
        Path(os.environ.get("ESO_VALUES", root / "values.yaml")).read_text()
    )
    assert values["installCRDs"] is True
    assert values["processSecretStore"] is True
    for key in (
        "processClusterStore",
        "processClusterExternalSecret",
        "processClusterGenerator",
        "processPushSecret",
        "processClusterPushSecret",
    ):
        assert values[key] is False
    for component in (values, values["webhook"], values["certController"]):
        resources = component["resources"]
        assert resources["requests"]["cpu"]
        assert resources["requests"]["memory"]
        assert resources["limits"]["memory"] == resources["requests"]["memory"]
    assert values["webhook"]["create"] is True
    assert values["webhook"]["port"] == 10250
    assert values["certController"]["create"] is True
    assert not any(values["rbac"].values())


def test_cilium_allows_only_the_api_server_and_never_world(docs):
    policies = {
        name: doc
        for (kind, name), doc in docs.items()
        if kind in {"CiliumNetworkPolicy", "CiliumClusterwideNetworkPolicy"}
    }
    assert set(policies) == {"external-secrets-api", "external-secrets-webhook"}
    for policy in policies.values():
        assert policy["metadata"]["namespace"] == "external-secrets"
        assert "world" not in yaml.safe_dump(policy)
    api = policies["external-secrets-api"]["spec"]
    assert set(api) == {"endpointSelector", "egress"}
    assert api["endpointSelector"]["matchExpressions"] == [
        {
            "key": "app.kubernetes.io/name",
            "operator": "In",
            "values": [
                "external-secrets",
                "external-secrets-webhook",
                "external-secrets-cert-controller",
            ],
        }
    ]
    assert api["egress"] == [
        {
            "toEntities": ["kube-apiserver"],
            "toPorts": [
                {
                    "ports": [
                        {"port": "6443", "protocol": "TCP"},
                        {"port": "443", "protocol": "TCP"},
                    ]
                }
            ],
        }
    ]
    webhook = policies["external-secrets-webhook"]["spec"]
    assert set(webhook) == {"endpointSelector", "ingress"}
    assert webhook["endpointSelector"]["matchLabels"] == {
        "app.kubernetes.io/instance": "external-secrets",
        "app.kubernetes.io/name": "external-secrets-webhook",
    }
    assert webhook["ingress"] == [
        {
            "fromEntities": ["kube-apiserver"],
            "toPorts": [{"ports": [{"port": "10250", "protocol": "TCP"}]}],
        }
    ]
