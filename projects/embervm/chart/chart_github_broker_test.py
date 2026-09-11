"""GitHub grants cannot be enabled with implicit identity or secret defaults."""

import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml


def render(tmp_path, github=None, spiffe=None):
    values = tmp_path / "values.yaml"
    values.write_text(
        yaml.safe_dump(
            {
                "tokenBroker": {
                    "githubApp": github or {},
                    "spiffe": spiffe or {},
                }
            }
        )
    )
    return subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "bosun",
            str(Path(__file__).resolve().parent),
            "--namespace",
            "embervm",
            "-f",
            str(values),
        ],
        capture_output=True,
        text=True,
    )


def configuration():
    caller = "spiffe://embervm.jomcgi.dev/ns/monolith/sa/review-publisher"
    return {
        "enabled": True,
        "appID": "123",
        "installationID": "456",
        "onepassword": {"itemPath": "vaults/test/items/bosun"},
        "grants": [
            {
                "name": "bosun-publisher",
                "profile": "review-publisher",
                "repositoryIDs": [789],
                "allowedSpiffeIds": [caller],
            }
        ],
        "clientPodSelectors": [
            {
                "matchLabels": {
                    "k8s:io.kubernetes.pod.namespace": "monolith",
                    "app.kubernetes.io/component": "review-publisher",
                }
            }
        ],
    }, {"enabled": True, "clientSpiffeIds": [caller]}


def test_disabled_does_not_mount_app_credentials(tmp_path):
    result = render(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "GITHUB_APP_PRIVATE_KEY" not in result.stdout
    assert "onepassworditem-github-app.yaml" not in result.stdout


def test_enabled_scopes_secret_and_network_to_broker(tmp_path):
    github, spiffe = configuration()
    result = render(tmp_path, github, spiffe)
    assert result.returncode == 0, result.stderr
    documents = list(yaml.safe_load_all(result.stdout))
    owners = []
    for doc in documents:
        for container in (
            (doc or {})
            .get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        ):
            env = {e["name"]: e for e in container.get("env", [])}
            if "GITHUB_APP_PRIVATE_KEY" in env:
                owners.append(container["name"])
                ref = env["GITHUB_APP_PRIVATE_KEY"]["valueFrom"]["secretKeyRef"]
                assert ref["name"].endswith("-github-app")
                assert ref["key"] == "private-key"
                assert json.loads(env["GITHUB_APP_GRANTS"]["value"]) == github["grants"]
    assert owners == ["tokenbroker"]
    assert "matchName: api.github.com" in result.stdout
    assert "vaults/test/items/bosun" in result.stdout
    assert "embervm-oauth-grant-bosun-publisher" not in result.stdout


@pytest.mark.parametrize(
    "missing, expected",
    [
        ("tls", "requires tokenBroker.spiffe.enabled"),
        ("callers", "requires explicit tokenBroker.spiffe.clientSpiffeIds"),
        ("grants", "requires explicit grants"),
        ("appID", "appID is required"),
        ("installationID", "installationID is required"),
        ("onepassword", "onepassword.itemPath is required"),
        ("clientPodSelectors", "requires clientPodSelectors"),
    ],
)
def test_incomplete_configuration_fails_render(tmp_path, missing, expected):
    github, spiffe = configuration()
    if missing == "tls":
        spiffe["enabled"] = False
    elif missing == "callers":
        spiffe["clientSpiffeIds"] = []
    elif missing == "onepassword":
        github["onepassword"]["itemPath"] = ""
    else:
        github[missing] = [] if missing in ("grants", "clientPodSelectors") else ""
    result = render(tmp_path, github, spiffe)
    assert result.returncode != 0
    assert expected in result.stderr


def canary_configuration():
    github, spiffe = configuration()
    caller = "spiffe://embervm.jomcgi.dev/ns/embervm/sa/bosun-embervm-github-canary"
    spiffe["clientSpiffeIds"].append(caller)
    github["canary"] = {
        "enabled": True,
        "grant": "bosun-canary",
        "deniedGrant": "bosun-publisher",
        "repositoryID": 789,
    }
    github["grants"].append(
        {
            "name": "bosun-canary",
            "profile": "reviewer",
            "repositoryIDs": [789],
            "allowedSpiffeIds": [caller],
        }
    )
    return github, spiffe, caller


def test_canary_has_own_identity_and_no_key_or_kubernetes_token(tmp_path):
    github, spiffe, _ = canary_configuration()
    result = render(tmp_path, github, spiffe)
    assert result.returncode == 0, result.stderr
    docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
    job = next(d for d in docs if d["kind"] == "Job")
    assert job["metadata"]["annotations"] == {
        "argocd.argoproj.io/hook": "PostSync",
        "argocd.argoproj.io/hook-delete-policy": "BeforeHookCreation",
    }
    pod = job["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "bosun-embervm-github-canary"
    assert pod["automountServiceAccountToken"] is False
    assert pod["restartPolicy"] == "Never"
    assert job["spec"]["backoffLimit"] == 0
    container = pod["containers"][0]
    assert container["args"] == ["--github-canary"]
    env = {e["name"]: e for e in container["env"]}
    assert (
        env["CANARY_BROKER_URL"]["value"]
        == "https://bosun-embervm-tokenbroker.embervm.svc:8443"
    )
    assert env["CANARY_REPOSITORY_ID"]["value"] == "789"
    assert "GITHUB_APP_PRIVATE_KEY" not in env
    assert all("valueFrom" not in e for e in env.values())
    assert pod["volumes"] == [
        {
            "name": "spiffe-workload-api",
            "csi": {"driver": "csi.spiffe.io", "readOnly": True},
        }
    ]
    policy = next(
        d
        for d in docs
        if d["kind"] == "CiliumNetworkPolicy"
        and d["metadata"]["name"].endswith("-github-canary")
    )
    assert policy["spec"]["ingress"] == []
    assert policy["spec"]["egress"][-1]["toFQDNs"] == [{"matchName": "api.github.com"}]


@pytest.mark.parametrize(
    "invalid",
    [
        "writer",
        "publisher",
        "repository",
        "listener",
        "missing_denied",
        "disabled_broker",
    ],
)
def test_canary_rejects_unsafe_grants(tmp_path, invalid):
    github, spiffe, caller = canary_configuration()
    if invalid == "writer":
        github["grants"][1]["profile"] = "implementer"
    elif invalid == "publisher":
        github["grants"][0]["allowedSpiffeIds"].append(caller)
    elif invalid == "repository":
        github["grants"][1]["repositoryIDs"].append(456)
    elif invalid == "listener":
        spiffe["clientSpiffeIds"].remove(caller)
    elif invalid == "missing_denied":
        github["canary"]["deniedGrant"] = "missing"
    elif invalid == "disabled_broker":
        github["enabled"] = False
    result = render(tmp_path, github, spiffe)
    assert result.returncode != 0
    assert "GitHub canary" in result.stderr


def test_canary_disabled_is_inert(tmp_path):
    github, spiffe = configuration()
    result = render(tmp_path, github, spiffe)
    assert result.returncode == 0, result.stderr
    assert "kind: Job" not in result.stdout
    assert "CANARY_BROKER_URL" not in result.stdout
