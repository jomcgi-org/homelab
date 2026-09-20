"""Guards for the inactive GKE store-validation configuration in #6193."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import yaml

DEV_BUCKET = "h0melab-ember-bases-dev"
PROD_BUCKET = "h0melab-ember-bases"
STORE_SECRET = "embervm-store-validation-gcs"
STORAGE_ENDPOINT = "https://storage.googleapis.com"
STORAGE_SERVICE = "services/95FF-2EF5-5EA1"


def _path(name: str) -> Path:
    return Path(os.environ[name])


def _load_yaml(name: str) -> dict:
    return yaml.safe_load(_path(name).read_text())


def _render_validation_release() -> list[dict]:
    command = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "embervm-store-validation",
        str(Path(__file__).resolve().parent),
        "--namespace",
        "embervm-store-validation",
        "--values",
        str(Path(__file__).resolve().parent / "values.yaml"),
        "--values",
        str(_path("DEV_VALUES")),
        "--values",
        str(_path("STORE_VALIDATION_VALUES")),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if isinstance(doc, dict)]


def _pod_containers(document: dict) -> list[dict]:
    pod_spec = document.get("spec", {}).get("template", {}).get("spec", {})
    return pod_spec.get("containers", []) + pod_spec.get("initContainers", [])


def _env(container: dict) -> dict[str, dict]:
    return {entry["name"]: entry for entry in container.get("env", [])}


def _strings(value) -> list[str]:
    if isinstance(value, dict):
        return [item for child in value.values() for item in _strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _strings(child)]
    return [value] if isinstance(value, str) else []


def _assert_required_secret_ref(entry: dict) -> None:
    secret_ref = entry["valueFrom"]["secretKeyRef"]
    assert secret_ref["name"] == STORE_SECRET
    assert secret_ref.get("optional", False) is False


def test_validation_overlay_is_unreferenced_and_production_is_unchanged() -> None:
    overlay = _load_yaml("STORE_VALIDATION_VALUES")
    store = overlay["noded"]["store"]
    assert store["endpoint"] == STORAGE_ENDPOINT
    assert store["bucket"] == DEV_BUCKET
    assert store["credentials"] == {
        "enabled": True,
        "secretName": STORE_SECRET,
        "onepassword": {"itemPath": ""},
    }

    for name in (
        "PROD_APPLICATION",
        "DEV_APPLICATION",
        "PROD_KUSTOMIZATION",
        "DEV_KUSTOMIZATION",
    ):
        assert "values-store-validation-gke.yaml" not in _path(name).read_text()

    production = _load_yaml("PROD_GKE_VALUES")
    assert production["noded"]["store"]["bucket"] == PROD_BUCKET
    assert DEV_BUCKET not in _path("PROD_GKE_VALUES").read_text()


def test_validation_render_routes_every_store_consumer_to_dev() -> None:
    documents = _render_validation_release()
    strings = _strings(documents)
    assert PROD_BUCKET not in strings
    assert DEV_BUCKET in strings
    assert not [
        doc
        for doc in documents
        if doc.get("kind") == "OnePasswordItem"
        and doc.get("metadata", {}).get("name") == STORE_SECRET
    ]

    control_planes = []
    nodeds = []
    rootfs_builders = []
    for document in documents:
        for container in _pod_containers(document):
            env = _env(container)
            if "EMBERVM_STORE_BUCKET" in env:
                control_planes.append(env)
            if container.get("name") == "noded":
                nodeds.append(env)
            if container.get("name", "").startswith("build-") and container.get(
                "name", ""
            ).endswith("-rootfs"):
                rootfs_builders.append(env)

    assert control_planes
    assert nodeds
    assert rootfs_builders
    for env in control_planes:
        assert env["EMBERVM_STORE_ENDPOINT"]["value"] == STORAGE_ENDPOINT
        assert env["EMBERVM_STORE_BUCKET"]["value"] == DEV_BUCKET
        _assert_required_secret_ref(env["EMBERVM_STORE_ACCESS_KEY_ID"])
        _assert_required_secret_ref(env["EMBERVM_STORE_SECRET_ACCESS_KEY"])
    for env in nodeds + rootfs_builders:
        assert env["EMBERVM_NODED_STORE_ENDPOINT"]["value"] == STORAGE_ENDPOINT
        assert env["EMBERVM_NODED_STORE_BUCKET"]["value"] == DEV_BUCKET
        _assert_required_secret_ref(env["EMBERVM_NODED_STORE_ACCESS_KEY_ID"])
        _assert_required_secret_ref(env["EMBERVM_NODED_STORE_SECRET_ACCESS_KEY"])


def test_dev_lifecycle_is_the_only_delete_definition() -> None:
    lifecycle = json.loads(_path("STORE_LIFECYCLE").read_text())
    assert lifecycle == {
        "rule": [{"action": {"type": "Delete"}, "condition": {"age": 7}}]
    }
    assert DEV_BUCKET in _path("STORE_LIFECYCLE").name
    runbook = _path("STORE_RUNBOOK").read_text()
    assert 'gcloud storage buckets update "gs://$validation_bucket"' in runbook
    assert "--lifecycle-file=" in runbook
    assert "Production remains on" in runbook


def test_budget_schema_is_alert_only_and_cloud_storage_scoped() -> None:
    specification = json.loads(_path("STORE_BUDGET").read_text())
    assert specification["schemaVersion"] == 1
    assert specification["provider"] == {
        "api": "billingbudgets.googleapis.com/v1",
        "cloudStorageService": STORAGE_SERVICE,
    }
    assert specification["operatorInputs"] == {
        "billingAccount": "billingAccounts/OPERATOR_BILLING_ACCOUNT_ID",
        "project": "projects/OPERATOR_PROJECT_NUMBER",
        "notificationChannel": (
            "projects/OPERATOR_PROJECT_NUMBER/notificationChannels/"
            "OPERATOR_NOTIFICATION_CHANNEL_ID"
        ),
    }

    budget = specification["budget"]
    assert budget["budgetFilter"] == {
        "calendarPeriod": "MONTH",
        "projects": ["projects/OPERATOR_PROJECT_NUMBER"],
        "services": [STORAGE_SERVICE],
    }
    assert budget["amount"] == {
        "specifiedAmount": {"currencyCode": "USD", "units": "15"}
    }
    assert budget["thresholdRules"] == [
        {"thresholdPercent": 1.0, "spendBasis": "CURRENT_SPEND"}
    ]
    assert budget["allUpdatesRule"] == {
        "monitoringNotificationChannels": [
            (
                "projects/OPERATOR_PROJECT_NUMBER/notificationChannels/"
                "OPERATOR_NOTIFICATION_CHANNEL_ID"
            )
        ],
        "disableDefaultIamRecipients": True,
    }
    assert "pubsubTopic" not in budget["allUpdatesRule"]
    runbook = _path("STORE_RUNBOOK").read_text()
    assert "entire Cloud Storage service" in runbook
    assert "does not cap or stop spending" in runbook
