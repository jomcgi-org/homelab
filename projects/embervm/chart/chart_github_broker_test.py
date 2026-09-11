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
