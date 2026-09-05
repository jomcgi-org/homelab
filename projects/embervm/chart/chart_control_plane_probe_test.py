"""Render-test the control-plane probe margin from issue #5235."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml


def _chart_dir() -> Path:
    chart = Path(__file__).resolve().parent
    if (chart / "Chart.yaml").exists():
        return chart
    raise RuntimeError("Could not find chart Chart.yaml")


def _control_plane_container() -> dict:
    helm_bin = os.environ.get("HELM_BIN", "helm")
    result = subprocess.run(
        [
            helm_bin,
            "template",
            "probe-test",
            str(_chart_dir()),
            "--show-only",
            "templates/deployment.yaml",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")

    deployment = next(
        document
        for document in yaml.safe_load_all(result.stdout)
        if document and document.get("kind") == "Deployment"
    )
    containers = deployment["spec"]["template"]["spec"]["containers"]
    return next(
        container for container in containers if container["name"] == "control-plane"
    )


def test_control_plane_liveness_allows_sixty_seconds_without_changing_readiness() -> (
    None
):
    container = _control_plane_container()
    liveness = container["livenessProbe"]
    readiness = container["readinessProbe"]

    assert liveness["failureThreshold"] == 6
    assert liveness["periodSeconds"] == 10
    assert liveness["httpGet"]["path"] == "/livez"
    assert liveness["timeoutSeconds"] == 5
    assert readiness.get("failureThreshold", 3) == 3
    assert readiness["periodSeconds"] == 5
    assert readiness["httpGet"]["path"] == "/healthz"
    assert readiness["timeoutSeconds"] == 2
