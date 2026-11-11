"""Pin the hub enrollment and default-off renders without using credentials."""

from __future__ import annotations

import copy
import os
import re
import subprocess
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
GKE = ROOT / "projects" / "platform-gke"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _render(*values_files: str) -> list[dict]:
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "renovate",
        str(HERE),
        "--namespace",
        "monolith-workflows",
    ]
    for name in values_files:
        argv.extend(["--values", str(HERE / name)])
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, f"helm template failed:\n{result.stderr}"
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _cron_suspensions(docs: list[dict]) -> dict[str, bool]:
    return {
        doc["metadata"]["name"]: doc["spec"]["suspend"]
        for doc in docs
        if doc.get("kind") == "CronWorkflow"
    }


def _named_target(text: str, name: str) -> str:
    for match in re.finditer(r"(?ms)^[a-z_][a-z0-9_]*\(\n.*?^\)\n", text):
        target = match.group(0)
        if re.search(rf'^    name = "{re.escape(name)}",$', target, re.MULTILINE):
            return target
    raise AssertionError(f"BUILD target {name!r} not found")


def test_base_render_remains_available_without_a_deployment_enrollment():
    target = _named_target((HERE / "BUILD").read_text(), "renovate")
    assert re.search(r'values_files = \[\n        "values.yaml",\n    \]', target)
    assert _cron_suspensions(_render("values.yaml")) == {
        "renovate": False,
        "renovate-apko-lock-maintenance": False,
    }


def test_hub_application_orders_overlay_last_and_renders_suspended():
    application = _load(GKE / "renovate" / "application.yaml")
    assert application["metadata"]["name"] == "renovate"
    assert application["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "2"
    assert application["spec"]["destination"] == {
        "server": "https://kubernetes.default.svc",
        "namespace": "monolith-workflows",
    }
    assert application["spec"]["source"] == {
        "repoURL": "https://github.com/jomcgi-org/homelab.git",
        "targetRevision": "HEAD",
        "path": "projects/platform/renovate",
        "helm": {
            "releaseName": "renovate",
            "valueFiles": ["values.yaml", "values-gke.yaml"],
            "ignoreMissingValueFiles": False,
        },
    }
    docs = _render(*application["spec"]["source"]["helm"]["valueFiles"])
    assert _cron_suspensions(docs) == {
        "renovate": True,
        "renovate-apko-lock-maintenance": True,
    }


def test_hub_overlay_suspends_writers_and_disables_automerge():
    assert _load(HERE / "values-gke.yaml") == {
        "suspend": True,
        "apkoLockMaintenance": {"suspend": True, "automerge": False},
    }
    base = _render("values.yaml")
    expected = copy.deepcopy(base)
    for doc in expected:
        if doc.get("kind") == "CronWorkflow":
            doc["spec"]["suspend"] = True
    assert _render("values.yaml", "values-gke.yaml") == expected


def test_hub_preserves_resource_names_schedules_and_credential_references():
    docs = _render("values.yaml", "values-gke.yaml")
    assert {(doc["kind"], doc["metadata"]["name"]) for doc in docs} == {
        ("ConfigMap", "renovate-apko-lock-maintenance"),
        ("CronWorkflow", "renovate"),
        ("CronWorkflow", "renovate-apko-lock-maintenance"),
        ("OnePasswordItem", "renovate-github"),
    }
    assert all(doc["metadata"]["namespace"] == "monolith-workflows" for doc in docs)
    items = [doc for doc in docs if doc["kind"] == "OnePasswordItem"]
    assert len(items) == 1
    assert items[0]["spec"]["itemPath"] == "vaults/k8s-homelab/items/renovate-github"
    schedules = {
        "renovate": ["0 4 * * *"],
        "renovate-apko-lock-maintenance": ["0 1 * * 1"],
    }
    for doc in docs:
        if doc["kind"] != "CronWorkflow":
            continue
        assert doc["spec"]["schedules"] == schedules[doc["metadata"]["name"]]
        assert doc["spec"]["timezone"] == "America/Vancouver"
        container = doc["spec"]["workflowSpec"]["templates"][0]["container"]
        assert [env for env in container["env"] if env["name"] == "RENOVATE_TOKEN"] == [
            {
                "name": "RENOVATE_TOKEN",
                "valueFrom": {
                    "secretKeyRef": {"name": "renovate-github", "key": "RENOVATE_TOKEN"}
                },
            }
        ]


def test_hub_root_reaches_the_renovate_application():
    cluster = ROOT / "projects" / "gke-cluster"
    parent = _load(cluster / "hub-root-application.yaml")
    assert parent["spec"]["source"]["path"] == "projects/gke-cluster/root"
    root = _load(cluster / "root" / "root-application.yaml")
    assert root["spec"]["source"]["path"] == "projects/gke-cluster"
    assert _load(cluster / "kustomization.yaml")["resources"] == [
        "../../projects/platform-gke",
        "../../projects/gke-apps",
    ]
    assert "./renovate" in _load(GKE / "kustomization.yaml")["resources"]
    assert _load(GKE / "renovate" / "kustomization.yaml")["resources"] == [
        "application.yaml"
    ]


def test_hub_enrollment_and_build_render_use_the_same_overlay_order():
    target = _named_target((HERE / "BUILD").read_text(), "renovate-gke")
    assert re.search(
        r'values_files = \[\n        "values.yaml",\n        "values-gke.yaml",\n    \]',
        target,
    )
