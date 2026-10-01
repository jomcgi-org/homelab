"""Render assertions for the chat_public retention and takedown CronWorkflows."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
import yaml


def _render() -> list[dict]:
    chart_dir = Path(__file__).resolve().parent
    result = subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "monolith",
            str(chart_dir),
            "--namespace",
            "monolith",
            "--set",
            "jobs.image.repository=registry.invalid/jobs",
            "--set-string",
            "jobs.image.digest=sha256:test",
            # publish-facts only renders when its lane is enabled.
            "--set",
            "knowledge.publishFacts.enabled=true",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


@pytest.fixture(scope="module")
def documents() -> list[dict]:
    return _render()


def _cron(documents: list[dict], name: str) -> dict:
    return next(
        doc
        for doc in documents
        if doc.get("kind") == "CronWorkflow" and doc["metadata"]["name"] == name
    )


def test_retention_is_scheduled_daily_and_active(documents):
    spec = _cron(documents, "chat-public-retention")["spec"]
    assert spec["schedules"] == ["15 4 * * *"]
    assert spec["suspend"] is False
    assert spec["concurrencyPolicy"] == "Forbid"
    assert spec["workflowSpec"]["activeDeadlineSeconds"] == 600
    container = spec["workflowSpec"]["templates"][0]["container"]
    assert container["args"] == ["chat-public-retention"]
    assert "arguments" not in spec["workflowSpec"]


def test_takedown_is_manual_only_with_parameters(documents):
    spec = _cron(documents, "chat-public-takedown")["spec"]
    assert spec["suspend"] is True
    assert spec["workflowSpec"]["arguments"]["parameters"] == [
        {"name": "session-id", "value": ""},
        {"name": "ip-hash", "value": ""},
    ]
    container = spec["workflowSpec"]["templates"][0]["container"]
    assert container["args"] == [
        "chat-public-takedown",
        "--session-id",
        "{{workflow.parameters.session-id}}",
        "--ip-hash",
        "{{workflow.parameters.ip-hash}}",
    ]


def test_existing_cronworkflow_renders_without_arguments(documents):
    spec = _cron(documents, "publish-facts")["spec"]
    assert "arguments" not in spec["workflowSpec"]
