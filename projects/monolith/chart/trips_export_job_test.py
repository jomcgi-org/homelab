"""Daily export has its own signing secret and bounded Argo execution."""

import os
import subprocess
from pathlib import Path

import yaml


def render(*extra):
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
            "jobs.image.repository=registry.invalid/jobs",
            "--set-string",
            "jobs.image.digest=sha256:test",
            *extra,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def test_trips_export_is_not_scheduled_without_shared_s3():
    docs = render("--set", "sharedS3.enabled=false")
    names = {(doc.get("kind"), doc["metadata"]["name"]) for doc in docs}
    assert ("CronWorkflow", "trips-export") not in names
    assert ("OnePasswordItem", "trips-export-imgproxy-signing") not in names


def test_trips_export_schedule_storage_and_signer():
    docs = render()
    job = next(
        doc
        for doc in docs
        if doc.get("kind") == "CronWorkflow"
        and doc["metadata"]["name"] == "trips-export"
    )
    assert job["spec"]["schedules"] == ["0 3 * * *"]
    assert job["spec"]["concurrencyPolicy"] == "Forbid"
    assert job["spec"]["workflowSpec"]["activeDeadlineSeconds"] == 21600
    container = job["spec"]["workflowSpec"]["templates"][0]["container"]
    assert container["args"] == ["trips-export"]
    assert container["resources"]["limits"]["memory"] == "1Gi"
    env = {entry["name"]: entry for entry in container["env"]}
    assert env["TRIPS_IMGPROXY_URL"]["value"] == "https://jomcgi.dev/img"
    assert env["TRIPS_EXPORT_PUBLIC_ENABLED"]["value"] == "false"
    assert (
        env["S3_ACCESS_KEY_ID"]["valueFrom"]["secretKeyRef"]["name"] == "monolith-r2-s3"
    )
    assert "SEAWEEDFS_S3_ENDPOINT" in env
    secret = env["IMGPROXY_KEY"]["valueFrom"]["secretKeyRef"]["name"]
    item = next(
        doc
        for doc in docs
        if doc.get("kind") == "OnePasswordItem" and doc["metadata"]["name"] == secret
    )
    assert item["metadata"]["namespace"] == job["metadata"]["namespace"]
    assert item["spec"]["itemPath"].endswith("/trips-imgproxy-signing")
