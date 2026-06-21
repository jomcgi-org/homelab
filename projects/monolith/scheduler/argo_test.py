"""Unit tests for the Argo job-submission gate (scheduler.argo).

No cluster access: build_job_workflow is pure, and submit_job_workflow is
exercised against a fake KubernetesClient.
"""

from __future__ import annotations

from unittest import mock

import pytest

import scheduler.argo as argo


def test_jobs_use_argo_defaults_to_monolith(monkeypatch):
    monkeypatch.delenv("JOB_EXECUTOR", raising=False)
    assert argo.jobs_use_argo() is False
    monkeypatch.setenv("JOB_EXECUTOR", "argo")
    assert argo.jobs_use_argo() is True
    monkeypatch.setenv("JOB_EXECUTOR", "ARGO")
    assert argo.jobs_use_argo() is True
    monkeypatch.setenv("JOB_EXECUTOR", "monolith")
    assert argo.jobs_use_argo() is False


def test_build_job_workflow_manifest(monkeypatch):
    monkeypatch.setenv(
        "JOBS_IMAGE", "ghcr.io/jomcgi/homelab/projects/monolith/jobs@sha256:abc"
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.delenv("WORLDCUP_API_BASE", raising=False)
    monkeypatch.setenv("WORKFLOW_NAMESPACE", "monolith-workflows")

    body = argo.build_job_workflow(
        "worldcup-sim", ["worldcup-sim"], ["DATABASE_URL", "WORLDCUP_API_BASE"]
    )

    assert body["kind"] == "Workflow"
    assert body["apiVersion"] == "argoproj.io/v1alpha1"
    assert body["metadata"]["generateName"] == "worldcup-sim-"
    assert body["metadata"]["namespace"] == "monolith-workflows"

    spec = body["spec"]
    assert spec["serviceAccountName"] == "argo-workflow"
    assert spec["ttlStrategy"]["secondsAfterCompletion"] == 3600

    template = spec["templates"][0]
    container = template["container"]
    assert container["image"].endswith("@sha256:abc")
    assert container["args"] == ["worldcup-sim"]
    # retryStrategy is a template-level field in Argo (sibling of container).
    assert template["retryStrategy"]["retryPolicy"] == "OnError"

    # Only env keys that are actually set are forwarded; the value is the literal
    # from this process (so the workflow namespace needs no secret copy).
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env == {"DATABASE_URL": "postgresql://u:p@h/db"}


def test_build_job_workflow_requires_jobs_image(monkeypatch):
    monkeypatch.delenv("JOBS_IMAGE", raising=False)
    with pytest.raises(KeyError):
        argo.build_job_workflow("worldcup-sim", ["worldcup-sim"], [])


@pytest.mark.asyncio
async def test_submit_job_workflow_calls_create(monkeypatch):
    monkeypatch.setenv("JOBS_IMAGE", "img@sha256:x")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("WORKFLOW_NAMESPACE", "monolith-workflows")
    captured = {}

    class FakeClient:
        async def create_workflow(self, namespace, body):
            captured["namespace"] = namespace
            captured["body"] = body
            return "worldcup-sim-abcde"

    with mock.patch("cluster.api.KubernetesClient", return_value=FakeClient()):
        name = await argo.submit_job_workflow(
            "worldcup-sim", ["worldcup-sim"], ["DATABASE_URL"]
        )

    assert name == "worldcup-sim-abcde"
    assert captured["namespace"] == "monolith-workflows"
    assert captured["body"]["kind"] == "Workflow"
