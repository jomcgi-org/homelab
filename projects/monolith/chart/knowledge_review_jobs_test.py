"""Rendering a deployment must not opt in to knowledge mutation jobs."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent
DEPLOY = Path(os.environ.get("DEPLOY_VALUES", CHART.parent / "deploy/values.yaml"))
GKE = Path(os.environ.get("GKE_VALUES", CHART.parent / "deploy/values-gke.yaml"))


def _render(*values: Path) -> dict[str, dict]:
    command = [
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
    ]
    for path in values:
        command.extend(["--values", str(path)])
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr
    return {
        doc["metadata"]["name"]: doc["spec"]
        for doc in yaml.safe_load_all(result.stdout)
        if isinstance(doc, dict) and doc.get("kind") == "CronWorkflow"
    }


@pytest.mark.parametrize("values", [(), (DEPLOY,), (DEPLOY, GKE)])
def test_review_mutation_jobs_are_suspended_by_default(values):
    jobs = _render(*values)
    # Force the jobs image above so a missing CronWorkflow cannot pass vacuously.
    for name in ("knowledge-review-backfill", "knowledge-review-admission"):
        assert jobs[name]["suspend"] is True
    backfill = jobs["knowledge-review-backfill"]
    assert backfill["schedules"] == ["*/5 * * * *"]
    assert backfill["concurrencyPolicy"] == "Forbid"
    workflow = backfill["workflowSpec"]
    assert workflow["activeDeadlineSeconds"] == 300
    assert workflow["templates"][0]["container"]["args"] == [
        "knowledge-review-backfill",
        "--apply",
        "--pending-only",
    ]


def test_backfill_requires_explicit_opt_in_without_enabling_admission(tmp_path):
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    entries = values["jobs"]["cronWorkflows"]
    backfill = next(
        job for job in entries if job["name"] == "knowledge-review-backfill"
    )
    assert backfill["suspend"] is True
    backfill["suspend"] = False
    override = tmp_path / "approved-backfill.yaml"
    # Helm replaces arrays. Preserve every other job's settings in this override.
    override.write_text(yaml.safe_dump({"jobs": {"cronWorkflows": entries}}))
    jobs = _render(DEPLOY, GKE, override)
    assert jobs["knowledge-review-backfill"]["suspend"] is False
    assert jobs["knowledge-review-admission"]["suspend"] is True
