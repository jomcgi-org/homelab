"""Disabled deployment renders and hermetic staged-sidecar wiring."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent


def render(overlay, enabled=False):
    args = [os.environ.get("HELM_BIN", "helm"), "template", "tts", str(CHART)]
    if overlay:
        args += [
            "-f",
            os.environ.get("DEPLOY_VALUES", str(CHART.parent / "deploy/values.yaml")),
        ]
    if overlay == "gke":
        args += [
            "-f",
            os.environ.get("GKE_VALUES", str(CHART.parent / "deploy/values-gke.yaml")),
        ]
    if enabled:
        args += [
            "--set",
            "grimoire.tts.enabled=true",
            "--set",
            "grimoire.tts.cacheEnabled=true",
        ]
    output = subprocess.run(
        args, capture_output=True, text=True, check=True, timeout=60
    )
    return [d for d in yaml.safe_load_all(output.stdout) if d]


@pytest.mark.parametrize("overlay", [None, "deploy", "gke"])
@pytest.mark.parametrize("enabled", [False, True])
def test_staged_workload_and_configuration(overlay, enabled):
    docs = render(overlay, enabled)
    deployment = next(
        d
        for d in docs
        if d["kind"] == "Deployment"
        and any(
            c["name"] == "backend" for c in d["spec"]["template"]["spec"]["containers"]
        )
    )
    pod = deployment["spec"]["template"]["spec"]
    containers = {c["name"]: c for c in pod["containers"]}
    env = {e["name"]: e.get("value") for e in containers["backend"]["env"]}
    assert env["GRIMOIRE_TTS_ENABLED"] == str(enabled).lower()
    assert env["GRIMOIRE_TTS_CACHE_ENABLED"] == str(enabled).lower()
    assert ("grimoire-tts" in containers) == enabled
    assert any(v["name"] == "tts-tmp" for v in pod["volumes"]) == enabled
    assert not any(
        "tts" in d["metadata"]["name"]
        and d["kind"] in ("CronWorkflow", "CronJob", "Job")
        for d in docs
    )
    if enabled:
        worker = containers["grimoire-tts"]
        assert "env" not in worker and "envFrom" not in worker
        assert worker["resources"]["limits"] == {"cpu": "1", "memory": "128Mi"}
        assert worker["securityContext"]["readOnlyRootFilesystem"] is True
        assert worker["volumeMounts"] == [{"name": "tts-tmp", "mountPath": "/tmp"}]
        assert "ports" not in worker
