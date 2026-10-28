"""Render real gated credential declarations, without creating any resources."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[1] / "chart"
ENABLED = {
    "grimoire.enabled": "true",
    "grimoire.play.enabled": "true",
    "grimoire.transcript.enabled": "true",
    "grimoire.transcript.google.project": "existing-project",
    "grimoire.transcript.google.onepassword.itemPath": "vaults/test/items/not-created",
}


def render(values):
    command = [os.environ["HELM_BIN"], "template", "stt", str(CHART)]
    for key, value in values.items():
        command.extend(["--set", f"{key}={value}"])
    return subprocess.run(command, check=False, capture_output=True, text=True)


@pytest.mark.parametrize("gate", [None, *list(ENABLED)[:3]])
def test_disabled_credentials_absent(gate):
    values = {**ENABLED, gate: "false"} if gate else {}
    result = render(values)
    assert result.returncode == 0, result.stderr
    assert "grimoire-stt" not in result.stdout
    assert "GRIMOIRE_STT_" not in result.stdout
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in result.stdout


def test_enabled_backend_only_credentials():
    result = render(ENABLED)
    assert result.returncode == 0, result.stderr
    documents = list(yaml.safe_load_all(result.stdout))
    credential = next(
        row
        for row in documents
        if row and row.get("metadata", {}).get("name", "").endswith("-grimoire-stt")
    )
    assert credential["kind"] == "OnePasswordItem"
    assert (
        credential["spec"]["itemPath"]
        == ENABLED["grimoire.transcript.google.onepassword.itemPath"]
    )
    deployments = [row for row in documents if row and row.get("kind") == "Deployment"]
    containers = [
        container
        for row in deployments
        for container in row["spec"]["template"]["spec"]["containers"]
    ]
    credential_consumers = [
        container
        for container in containers
        if any(
            env["name"] == "GOOGLE_APPLICATION_CREDENTIALS"
            for env in container.get("env", [])
        )
    ]
    assert len(credential_consumers) == 1
    backend = credential_consumers[0]
    assert {env["name"]: env.get("value") for env in backend["env"]}[
        "GRIMOIRE_STT_PROVIDER"
    ] == "google"
    assert (
        next(
            mount
            for mount in backend["volumeMounts"]
            if mount["name"] == "grimoire-stt"
        )["readOnly"]
        is True
    )
    assert all(
        not any(
            mount["name"] == "grimoire-stt"
            for mount in container.get("volumeMounts", [])
        )
        for container in containers
        if container is not backend
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("grimoire.transcript.provider", "other"),
        ("grimoire.transcript.google.project", ""),
        ("grimoire.transcript.google.onepassword.itemPath", ""),
        ("grimoire.transcript.google.credentialKey", "../credentials"),
    ],
)
def test_invalid_activation_fails(key, value):
    assert render({**ENABLED, key: value}).returncode != 0
