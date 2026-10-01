"""Guard Loom's staged hub Application, secret references and source paths."""

import copy
import os
import re
from pathlib import Path

import pytest
import yaml

DEPLOY = Path(__file__).resolve().parent
ROOT = DEPLOY.parents[2]
APPLICATION = Path(
    os.environ.get(
        "LOOM_APPLICATION", str(ROOT / "projects/gke-apps/loom/application.yaml")
    )
)
HUB = Path(
    os.environ.get(
        "HUB_KUSTOMIZATION", str(ROOT / "projects/gke-apps/kustomization.yaml")
    )
)
GATEWAY = Path(
    os.environ.get(
        "GATEWAY_APPLICATION",
        str(ROOT / "projects/gke-apps/context-forge-gateway/application.yaml"),
    )
)


def load(path):
    return yaml.safe_load(path.read_text())


def assert_sources(app):
    assert app["apiVersion"] == "argoproj.io/v1alpha1"
    assert app["kind"] == "Application"
    assert app["metadata"]["name"] == "loom"
    assert app["metadata"]["namespace"] == "argocd"
    assert app["metadata"]["finalizers"] == ["resources-finalizer.argocd.argoproj.io"]
    assert app["spec"]["project"] == "default"
    assert app["spec"]["sources"] == [
        {
            "repoURL": "ghcr.io/weave-hand/charts",
            "chart": "loom",
            "targetRevision": "0.2.0",
            "helm": {
                "releaseName": "loom",
                "valueFiles": ["$values/projects/loom/deploy/values.yaml"],
            },
        },
        {
            "repoURL": "https://github.com/jomcgi-org/homelab.git",
            "targetRevision": "HEAD",
            "ref": "values",
        },
        {
            "repoURL": "https://github.com/jomcgi-org/homelab.git",
            "targetRevision": "HEAD",
            "path": "projects/loom/deploy",
        },
    ]
    assert app["spec"]["destination"] == {
        "server": "https://kubernetes.default.svc",
        "namespace": "loom",
    }


def assert_default_off(hub):
    resources = hub["resources"]
    assert not any(
        Path(resource).as_posix().rstrip("/")
        in {
            "loom",
            "loom/application.yaml",
            "../loom/deploy",
        }
        for resource in resources
    )
    # Pin the complete existing root, including order. This prevents another
    # spelling or an added component from silently enrolling the Application.
    assert resources == [
        "namespace-embervm.yaml",
        "./embervm",
        "./embervm-dev",
        "./monolith",
        "./monolith-public",
        "./monolith-agents",
        "./inference",
        "./context-forge-gateway",
    ]
    assert set(hub) == {"apiVersion", "kind", "resources"}


def test_application_sources_and_values_path_exist():
    app = load(APPLICATION)
    assert_sources(app)
    value_path = app["spec"]["sources"][0]["helm"]["valueFiles"][0]
    assert (DEPLOY / Path(value_path).name).is_file()
    assert value_path.removeprefix("$values/") == "projects/loom/deploy/values.yaml"
    assert (DEPLOY / "kustomization.yaml").is_file()
    assert load(APPLICATION.parent / "kustomization.yaml")["resources"] == [
        "application.yaml"
    ]


def test_sync_policy_matches_hub_gateway_with_literal_retry():
    policy = load(APPLICATION)["spec"]["syncPolicy"]
    assert policy == load(GATEWAY)["spec"]["syncPolicy"]
    assert policy == {
        "automated": {"prune": True, "selfHeal": True},
        "syncOptions": ["CreateNamespace=true", "ServerSideApply=true"],
        "retry": {
            "limit": 5,
            "backoff": {"duration": "5s", "factor": 2, "maxDuration": "3m"},
        },
    }


def test_hub_default_off():
    assert_default_off(load(HUB))


def test_repository_credential_item_is_in_argocd_with_repository_label():
    item = load(DEPLOY / "argocd-repo-cred.yaml")
    assert item == {
        "apiVersion": "onepassword.com/v1",
        "kind": "OnePasswordItem",
        "metadata": {
            "name": "repo-weave-hand-charts",
            "namespace": "argocd",
            "labels": {"argocd.argoproj.io/secret-type": "repository"},
        },
        "spec": {"itemPath": "vaults/k8s-homelab/items/argocd-repo-weave-hand-charts"},
    }


def assert_no_credential_payload(value, path=()):
    if isinstance(value, dict):
        assert value.get("kind") != "Secret"
        for key, child in value.items():
            if key.lower() in {
                "password",
                "token",
                "privatekey",
                "private_key",
                "accesskeyid",
                "secretaccesskey",
                "aws_access_key_id",
                "aws_secret_access_key",
            }:
                references = {
                    ("postgres", "external", "keys", "password"): "password",
                    (
                        "objectStore",
                        "s3",
                        "credentialsKeys",
                        "accessKeyId",
                    ): "access-key-id",
                    (
                        "objectStore",
                        "s3",
                        "credentialsKeys",
                        "secretAccessKey",
                    ): "secret-access-key",
                }
                assert path + (key,) in references
                assert child == references[path + (key,)]
            if not path:
                assert key not in {"data", "stringData", "binaryData"}
            assert_no_credential_payload(child, path + (key,))
    elif isinstance(value, list):
        for child in value:
            assert_no_credential_payload(child, path)
    elif isinstance(value, str):
        assert "PRIVATE KEY" not in value
        assert not re.search(r"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}", value)
        assert not re.search(r"AKIA[A-Z0-9]{16}", value)
        assert not re.search(r"://[^\s/]+:[^\s/]+@", value)


def test_no_plaintext_credentials_in_repository_deployment_inputs():
    paths = list(DEPLOY.glob("*.yaml")) + list(APPLICATION.parent.glob("*.yaml"))
    for path in paths:
        assert ".svc.cluster.local" not in path.read_text()
        for document in yaml.safe_load_all(path.read_text()):
            assert_no_credential_payload(document)
    assert not (DEPLOY / "serviceaccount.yaml").exists()
    assert not (DEPLOY / "s3-credentials.yaml").exists()


@pytest.mark.parametrize("mutation", ["edge_chart", "hub_enrollment"])
def test_application_mutations_fail_acceptance(mutation):
    app = copy.deepcopy(load(APPLICATION))
    hub = copy.deepcopy(load(HUB))
    if mutation == "edge_chart":
        app["spec"]["sources"][0]["targetRevision"] = "0.0.0-edge"
    else:
        hub["resources"].append("./loom")
    with pytest.raises(AssertionError):
        assert_sources(app)
        assert_default_off(hub)
