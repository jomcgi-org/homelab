"""Preserve dependency eligibility and unconditional auto-merge prohibitions."""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


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
        # helmv3 uses the bare chart name as depName and the qualified OCI
        # identity as packageName. Both manager shapes must stay excluded.
        assert _dependency_is_disabled(config, chart, dep_name), (
            f"First-party Helm dependency enabled: {dep_name}"
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
            assert not _dependency_is_disabled(config, chart, upstream_name), (
                upstream_name
            )


def test_mirrored_helmv3_chart_stays_disabled_without_excluding_upstream():
    config = json.loads((ROOT / "renovate.json").read_text())
    # Renovate 44.14.10 helmv3/extract.ts keeps the Chart.yaml dependency's
    # bare depName. helmv3/utils.ts parseRepository qualifies OCI packageName.
    # context-forge-gateway/chart/Chart.yaml declares mcp-stack 1.0.7 from
    # oci://ghcr.io/jomcgi/homelab/charts, with a manual mirror update procedure.
    assert _dependency_is_disabled(
        config, "mcp-stack", "ghcr.io/jomcgi/homelab/charts/mcp-stack"
    ), "Mirrored mcp-stack must stay disabled by its helmv3 packageName"
    for package_name in (
        "mcp-stack",
        "ghcr.io/ibm/mcp-context-forge/mcp-stack",
        "ghcr.io/jomcgi/other/charts/mcp-stack",
        "ghcr.io/jomcgi/homelab/charts-other/mcp-stack",
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
        "First-party charts and the manual mcp-stack mirror are maintained outside Renovate.",
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
