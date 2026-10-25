"""The additive aurora producer stays suspended with existing storage identity."""

import os
import subprocess
from pathlib import Path

import yaml


def test_aurora_workflow_has_explicit_bounds_and_existing_storage_identity():
    chart = Path(__file__).resolve().parent
    result = subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "monolith",
            str(chart),
            "--namespace",
            "monolith",
            "--set",
            "jobs.image.repository=test.invalid/jobs",
            "--set",
            "jobs.image.digest=sha256:" + "0" * 64,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    workflow = next(
        doc
        for doc in yaml.safe_load_all(result.stdout)
        if doc and doc.get("metadata", {}).get("name") == "stars-aurora"
    )
    spec = workflow["spec"]
    assert spec["suspend"] is True
    assert spec["schedules"] == ["*/10 * * * *"]
    assert spec["concurrencyPolicy"] == "Forbid"
    runtime = spec["workflowSpec"]
    assert runtime["activeDeadlineSeconds"] == 240
    assert runtime["podPriorityClassName"] == "homelab-disposable"
    container = runtime["templates"][0]["container"]
    assert container["args"] == ["stars-aurora"]
    assert container["resources"]["limits"] == {"cpu": "500m", "memory": "512Mi"}
    env = {entry["name"]: entry for entry in container["env"]}
    assert (
        env["S3_ACCESS_KEY_ID"]["valueFrom"]["secretKeyRef"]["name"] == "monolith-r2-s3"
    )
    assert (
        env["S3_SECRET_ACCESS_KEY"]["valueFrom"]["secretKeyRef"]["name"]
        == "monolith-r2-s3"
    )
    assert env["STARS_GRID_S3_BUCKET"]["value"] == "stars"
