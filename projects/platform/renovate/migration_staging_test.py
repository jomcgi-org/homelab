"""Pin the staged, single-writer Renovate migration contract."""

from __future__ import annotations

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


def _onepassword_item(docs: list[dict]) -> dict:
    items = [doc for doc in docs if doc.get("kind") == "OnePasswordItem"]
    assert len(items) == 1
    return items[0]


def _named_target(text: str, name: str) -> str:
    for match in re.finditer(r"(?ms)^[a-z_][a-z0-9_]*\(\n.*?^\)\n", text):
        target = match.group(0)
        if re.search(rf'^    name = "{re.escape(name)}",$', target, re.MULTILINE):
            return target
    raise AssertionError(f"BUILD target {name!r} not found")


def test_home_merge_behavior_remains_enrolled_and_active():
    application = _load(HERE / "application.yaml")
    assert application["spec"]["source"]["helm"]["valueFiles"] == ["values.yaml"]
    assert "./renovate" in _load(HERE.parent / "kustomization.yaml")["resources"]

    docs = _render("values.yaml")
    assert _cron_suspensions(docs) == {
        "renovate": False,
        "renovate-apko-lock-maintenance": False,
    }


def test_hub_application_orders_staging_overlay_last_and_renders_suspended():
    application = _load(GKE / "renovate" / "application.yaml")
    assert application["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "2"
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

    docs = _render("values.yaml", "values-gke.yaml")
    assert _cron_suspensions(docs) == {
        "renovate": True,
        "renovate-apko-lock-maintenance": True,
    }
    assert _onepassword_item(docs)["spec"]["itemPath"] == (
        "vaults/k8s-homelab/items/renovate-github"
    )


def test_home_cutover_overlay_suspends_both_writers_without_other_overrides():
    overlay = _load(HERE / "values-home-suspend.yaml")
    assert overlay == {
        "suspend": True,
        "apkoLockMaintenance": {"suspend": True},
    }
    docs = _render("values.yaml", "values-home-suspend.yaml")
    assert _cron_suspensions(docs) == {
        "renovate": True,
        "renovate-apko-lock-maintenance": True,
    }


def test_cluster_overlays_are_isolated_until_the_operator_cutover():
    assert _load(HERE / "values-gke.yaml") == {
        "suspend": True,
        "apkoLockMaintenance": {"suspend": True},
    }
    home_application = _load(HERE / "application.yaml")
    gke_application = _load(GKE / "renovate" / "application.yaml")
    assert (
        "values-home-suspend.yaml"
        not in home_application["spec"]["source"]["helm"]["valueFiles"]
    )
    assert (
        "values-home-suspend.yaml"
        not in gke_application["spec"]["source"]["helm"]["valueFiles"]
    )


def test_hub_enrollment_and_build_render_use_the_same_overlay_order():
    assert "./renovate" in _load(GKE / "kustomization.yaml")["resources"]
    assert _load(GKE / "renovate" / "kustomization.yaml")["resources"] == [
        "application.yaml"
    ]

    target = _named_target((HERE / "BUILD").read_text(), "renovate-gke")
    assert re.search(
        r'values_files = \[\n        "values.yaml",\n        "values-gke.yaml",\n    \]',
        target,
    )
