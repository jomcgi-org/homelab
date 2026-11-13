"""Render the staged job with a populated jobs image, never deploy it."""

import os
from pathlib import Path
import subprocess

import pytest
import yaml

CHART = Path(__file__).parent


def render(*extra):
    result = subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "monolith",
            str(CHART),
            "--namespace",
            "monolith",
            "--set",
            "jobs.image.repository=registry.invalid/jobs",
            "--set-string",
            "jobs.image.digest=sha256:test",
            *extra,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def crons(docs):
    return {
        doc["metadata"]["name"]: doc
        for doc in docs
        if doc.get("kind") == "CronWorkflow"
    }


def test_default_is_absent_with_existing_jobs_preserved():
    jobs = crons(render())
    assert "sepa-collect" not in jobs
    assert jobs["chat-public-retention"]["spec"]["workflowSpec"]["templates"][0][
        "container"
    ]["args"] == ["chat-public-retention"]
    assert "snapshot-merged-prs" in jobs


def test_opt_in_wiring_bounds_and_shared_storage():
    job = crons(render("--set", "sepa.enabled=true"))["sepa-collect"]
    spec = job["spec"]
    assert spec["schedules"] == ["17 */3 * * *"]
    assert spec["concurrencyPolicy"] == "Forbid"
    assert spec["startingDeadlineSeconds"] == 30
    assert spec["workflowSpec"]["activeDeadlineSeconds"] == 300
    container = spec["workflowSpec"]["templates"][0]["container"]
    assert container["image"] == "registry.invalid/jobs@sha256:test"
    assert container["args"] == ["sepa-collect"]
    env = {entry["name"]: entry for entry in container["env"]}
    assert env["SEPA_ENABLED"]["value"] == "true"
    assert env["SEPA_STATION_NUMBERS"]["value"] == "133171,9514"
    assert env["SEPA_PREFIX"]["value"] == "collectors/sepa"
    assert env["STARS_GRID_S3_BUCKET"]["value"] == "stars"
    assert env["SEAWEEDFS_S3_ENDPOINT"]["value"].endswith(".r2.cloudflarestorage.com")
    assert (
        env["S3_ACCESS_KEY_ID"]["valueFrom"]["secretKeyRef"]["name"] == "monolith-r2-s3"
    )
    assert "DATABASE_URL" not in env
    assert container["resources"]["requests"]["memory"] == "256Mi"
    assert container["resources"]["limits"]["memory"] == "512Mi"


def test_gke_overlay_remains_off():
    deploy = os.environ.get(
        "DEPLOY_VALUES", str(CHART.parent / "deploy" / "values.yaml")
    )
    gke = os.environ.get("GKE_VALUES", str(CHART.parent / "deploy" / "values-gke.yaml"))
    assert "sepa-collect" not in crons(render("-f", deploy, "-f", gke))


@pytest.mark.parametrize(
    "setting",
    [
        "--set-string=sepa.enabled=true",
        "--set=sepa.enabled=true,sharedS3.enabled=false",
    ],
)
def test_invalid_or_unavailable_storage_fails_closed(setting):
    result = subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "monolith",
            str(CHART),
            setting,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "sepa.enabled" in result.stderr
