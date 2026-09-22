"""Render checks for tokenbroker's durable quota-state ownership contract."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml


def _chart_dir() -> Path:
    chart = Path(__file__).resolve().parent
    if not (chart / "Chart.yaml").exists():
        raise RuntimeError("Could not find chart Chart.yaml")
    return chart


def _render(release: str) -> list[dict]:
    result = subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            release,
            str(_chart_dir()),
            "--namespace",
            release,
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def _one(documents: list[dict], kind: str, name: str) -> dict:
    matches = [
        document
        for document in documents
        if document.get("kind") == kind
        and document.get("metadata", {}).get("name") == name
    ]
    assert len(matches) == 1
    return matches[0]


def test_quota_state_is_dedicated_durable_and_narrowly_scoped() -> None:
    release = "quota-state"
    name = f"{release}-embervm-tokenbroker-quota"
    documents = _render(release)

    state = _one(documents, "ConfigMap", name)
    assert state.get("data") == {}
    assert (
        state["metadata"]["annotations"]["argocd.argoproj.io/sync-options"]
        == "Prune=false,Delete=false"
    )

    role = _one(documents, "Role", f"{release}-embervm-tokenbroker")
    configmap_rules = [
        rule for rule in role["rules"] if rule.get("resources") == ["configmaps"]
    ]
    assert configmap_rules == [
        {
            "apiGroups": [""],
            "resources": ["configmaps"],
            "resourceNames": [name],
            "verbs": ["get", "update"],
        }
    ]

    deployment = _one(documents, "Deployment", f"{release}-embervm-tokenbroker")
    env = {
        item["name"]: item.get("value")
        for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["TOKENBROKER_QUOTA_CONFIGMAP"] == name


def test_every_argocd_application_preserves_runtime_quota_data() -> None:
    applications = [
        (Path(os.environ["PROD_APPLICATION"]), "embervm-tokenbroker-quota"),
        (Path(os.environ["GKE_APPLICATION"]), "embervm-tokenbroker-quota"),
        (Path(os.environ["DEV_APPLICATION"]), "embervm-dev-tokenbroker-quota"),
    ]
    for path, expected_name in applications:
        application = yaml.safe_load(path.read_text())
        entries = application["spec"]["ignoreDifferences"]
        assert {
            "kind": "ConfigMap",
            "name": expected_name,
            "jsonPointers": ["/data"],
        } in entries
