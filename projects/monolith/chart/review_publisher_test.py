from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml


def render(enabled: bool) -> list[dict]:
    chart = Path(__file__).resolve().parent
    result = subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "monolith",
            str(chart),
            "--set",
            "swarm.enabled=true",
            "--set",
            f"factory.reviewPublish.enabled={str(enabled).lower()}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def publisher_env(documents: list[dict]) -> list[dict]:
    deployment = next(
        document
        for document in documents
        if document.get("kind") == "Deployment"
        and document.get("metadata", {}).get("name") == "monolith"
    )
    containers = deployment["spec"]["template"]["spec"]["containers"]
    backend = next(
        container for container in containers if container["name"] == "backend"
    )
    return [
        item
        for item in backend.get("env", [])
        if item.get("name") == "FACTORY_REVIEW_PUBLISH_ENABLED"
    ]


def test_review_publisher_env_is_absent_when_disabled():
    assert publisher_env(render(False)) == []


def test_review_publisher_env_is_true_without_a_secret_when_enabled():
    assert publisher_env(render(True)) == [
        {"name": "FACTORY_REVIEW_PUBLISH_ENABLED", "value": "true"}
    ]
