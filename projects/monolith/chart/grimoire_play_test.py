"""Pin the default-off play flag through actual Helm rendering."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize(
    "deploy,enabled,expected",
    [
        (False, None, "false"),
        (True, None, "false"),
        (False, True, "true"),
        (True, True, "true"),
    ],
)
def test_play_env_is_quoted_and_default_off(deploy, enabled, expected):
    chart = Path(__file__).resolve().parent
    command = [os.environ.get("HELM_BIN", "helm"), "template", "kg", str(chart)]
    if deploy:
        command.extend(
            [
                "-f",
                os.environ.get(
                    "DEPLOY_VALUES", str(chart.parent / "deploy" / "values.yaml")
                ),
            ]
        )
    if enabled is not None:
        command.extend(["--set", "grimoire.play.enabled=true"])
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=120, check=True
    )
    deployments = [
        doc
        for doc in yaml.safe_load_all(result.stdout)
        if doc and doc.get("kind") == "Deployment"
    ]
    backend = next(
        container
        for doc in deployments
        for container in doc["spec"]["template"]["spec"]["containers"]
        if container["name"] == "backend"
    )
    values = [
        entry["value"]
        for entry in backend["env"]
        if entry["name"] == "GRIMOIRE_PLAY_ENABLED"
    ]
    assert values == [expected]
    assert isinstance(values[0], str)
