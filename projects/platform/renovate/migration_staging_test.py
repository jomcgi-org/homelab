"""Pin the staged, single-writer Renovate migration contract."""

from __future__ import annotations

import json
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


def test_all_renovate_update_types_keep_both_auto_merge_paths_disabled():
    config = json.loads((ROOT / "renovate.json").read_text())
    assert config["automerge"] is False
    assert config["platformAutomerge"] is False

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ("automerge", "platformAutomerge"):
                    assert child is False
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(config)


def _dependency_is_disabled(
    config: dict, dep_name: str, package_name: str | None = None
) -> bool:
    rules = [rule for rule in config["packageRules"] if rule.get("enabled") is False]
    disabled = False
    for rule in rules:
        # No additional matcher may narrow these exclusions. For literal names,
        # Renovate's matchRegexOrGlobList is a case-insensitive exact match.
        matchers = set(rule) - {"description", "enabled"}
        assert matchers in ({"matchDepNames"}, {"matchPackageNames"})
        matcher = next(iter(matchers))
        # Renovate defaults packageName to depName when extraction omits it.
        identity = dep_name if matcher == "matchDepNames" else package_name or dep_name
        for name in rule[matcher]:
            assert re.fullmatch(r"[a-zA-Z0-9:/._-]+", name), name
            disabled |= identity.casefold() == name.casefold()
    return disabled


def test_first_party_chart_exclusions_match_extracted_oci_names():
    config = json.loads((ROOT / "renovate.json").read_text())
    charts = (
        "embervm",
        "monolith",
        "monolith-public",
        "monolith-agents",
        "context-forge",
        "inference",
        "oci-model-cache-operator",
    )
    for chart in charts:
        # Renovate 44.14.10 lib/modules/manager/argocd/extract.ts processSource
        # emits registryURL/chart as depName with datasource=docker for an OCI
        # repoURL, including protocol-less ghcr.io/jomcgi/homelab/charts.
        dep_name = f"ghcr.io/jomcgi/homelab/charts/{chart}"
        assert _dependency_is_disabled(config, dep_name), (
            f"First-party OCI dependency enabled: {dep_name}"
        )

        # An upstream Helm chart with the same short name must stay eligible,
        # as must OCI charts outside this repository's exact registry path.
        for upstream_name in (
            chart,
            f"ghcr.io/upstream/charts/{chart}",
            f"ghcr.io/jomcgi/other/charts/{chart}",
            f"registry.example.com/jomcgi/homelab/charts/{chart}",
        ):
            assert not _dependency_is_disabled(config, upstream_name), upstream_name


def test_mirrored_helmv3_chart_stays_disabled_without_excluding_upstream():
    config = json.loads((ROOT / "renovate.json").read_text())
    # Renovate 44.14.10 helmv3/extract.ts keeps the Chart.yaml dependency's
    # bare depName. helmv3/utils.ts parseRepository qualifies OCI packageName.
    # This is the mcp-stack dependency in the context-forge-gateway chart.
    assert _dependency_is_disabled(
        config, "mcp-stack", "ghcr.io/jomcgi/homelab/charts/mcp-stack"
    ), "Mirrored mcp-stack must stay disabled by its helmv3 packageName"
    for package_name in (
        "mcp-stack",
        "ghcr.io/ibm/mcp-context-forge/mcp-stack",
        "ghcr.io/jomcgi/other/charts/mcp-stack",
        "registry.example.com/jomcgi/homelab/charts/mcp-stack",
    ):
        assert not _dependency_is_disabled(config, "mcp-stack", package_name), (
            package_name
        )


def test_upstream_chart_dependencies_remain_enabled():
    config = json.loads((ROOT / "renovate.json").read_text())
    # Extracted depName values for upstream OCI and HTTPS Helm sources.
    for dep_name in (
        "ghcr.io/weave-hand/charts/loom",
        "ghcr.io/ariga/charts/atlas-operator",
        "cloudnative-pg",
    ):
        assert not _dependency_is_disabled(config, dep_name), (
            f"Upstream chart dependency disabled: {dep_name}"
        )


def test_self_source_exclusions_use_current_organization():
    config = json.loads((ROOT / "renovate.json").read_text())
    description = (
        "Self-referential git sources (deploy values pulled from our own repo) "
        "are not upgrade targets."
    )
    rules = [
        rule for rule in config["packageRules"] if rule["description"] == description
    ]
    assert rules == [
        {
            "description": description,
            "matchDepNames": [
                "https://github.com/jomcgi-org/homelab",
                "https://github.com/jomcgi-org/homelab.git",
            ],
            "enabled": False,
        }
    ]


def test_active_managers_and_upstream_chart_updates_remain_available():
    config = json.loads((ROOT / "renovate.json").read_text())
    assert "dockerfile" not in config["enabledManagers"]
    assert config["enabledManagers"] == [
        "argocd",
        "bazel-module",
        "bazelisk",
        "custom.regex",
        "github-actions",
        "gomod",
        "helm-values",
        "helmv3",
        "kubernetes",
        "npm",
        "pep621",
    ]
    description = "Identify upstream chart updates in the broader dependency stream."
    assert [
        rule for rule in config["packageRules"] if rule["description"] == description
    ] == [
        {
            "description": description,
            "matchManagers": ["argocd", "helmv3"],
            "addLabels": ["upstream-chart"],
        }
    ]
    # Keep disabling rules confined to our charts, mirror and self-source URLs.
    assert [
        rule["description"]
        for rule in config["packageRules"]
        if rule.get("enabled") is False
    ] == [
        "First-party and mirrored charts are maintained outside Renovate.",
        "Self-referential git sources (deploy values pulled from our own repo) "
        "are not upgrade targets.",
    ]


def test_apko_lock_maintenance_does_not_request_auto_merge():
    script = (HERE / "templates" / "apko-lock-script.yaml").read_text()
    for forbidden in (
        "enablePullRequestAutoMerge",
        "mergeMethod",
        "--auto",
        "auto_merge",
        "platformAutomerge",
    ):
        assert forbidden not in script
    assert 'method: "POST"' in script
    assert 'labels: ["dependencies"]' in script
