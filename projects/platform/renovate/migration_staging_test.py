"""Pin the staged, single-writer Renovate migration contract."""

from __future__ import annotations

import fnmatch
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


def test_ordinary_updates_include_majors_and_use_platform_pr_automerge():
    config = json.loads((ROOT / "renovate.json").read_text())
    assert config["automerge"] is True
    assert config["platformAutomerge"] is True
    assert config["automergeType"] == "pr"
    assert config["automergeStrategy"] == "auto"
    assert config["dependencyDashboardApproval"] is False
    expected = {
        "major",
        "minor",
        "patch",
        "pin",
        "digest",
        "pinDigest",
        "lockFileMaintenance",
        "replacement",
        "bump",
        "rollback",
    }
    rules = [rule for rule in config["packageRules"] if rule.get("automerge")]
    assert len(rules) == 1
    assert set(rules[0]["matchUpdateTypes"]) == expected
    assert rules[0]["platformAutomerge"] is True
    assert config["minimumReleaseAge"] == "3 days"
    assert config["internalChecksFilter"] == "strict"
    assert config["lockFileMaintenance"]["enabled"] is True
    assert "ignoreTests" not in config
    assert config.get("statusCheckNames", ["default"]) is not None


def _dependency_is_disabled(
    config: dict,
    dep_name: str,
    package_name: str | None = None,
    datasource: str = "docker",
) -> bool:
    rules = [rule for rule in config["packageRules"] if rule.get("enabled") is False]
    disabled = False
    for rule in rules:
        if "matchManagers" in rule or "matchFileNames" in rule:
            continue  # These are extraction handoffs, tested separately below.
        if "matchDatasources" in rule and datasource not in rule["matchDatasources"]:
            continue
        matchers = set(rule) - {"description", "enabled", "matchDatasources"}
        assert matchers in ({"matchDepNames"}, {"matchPackageNames"})
        matcher = next(iter(matchers))
        identity = dep_name if matcher == "matchDepNames" else package_name or dep_name
        for name in rule[matcher]:
            # This helper covers only the literal and prefix-glob exclusions in
            # this config. Extraction is also checked with pinned Renovate.
            assert re.fullmatch(r"[a-zA-Z0-9:/*._-]+", name), name
            disabled |= fnmatch.fnmatchcase(identity.casefold(), name.casefold())
    return disabled


def test_first_party_chart_exclusions_match_extracted_oci_names():
    config = json.loads((ROOT / "renovate.json").read_text())
    charts = (
        "embervm",
        "monolith",
        "monolith-public",
        "monolith-agents",
        "scotland-connector",
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
        "bazel",
        "bazel-module",
        "custom.regex",
        "github-actions",
        "gomod",
        "helm-values",
        "helmv3",
        "kubernetes",
        "npm",
        "pep621",
        "pip_requirements",
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
    # Exact holds and first-party publishers are the only disabled dependencies.
    assert [
        rule["description"]
        for rule in config["packageRules"]
        if rule.get("enabled") is False
    ] == [
        "First-party charts and the manual mcp-stack mirror are maintained outside Renovate.",
        (
            "Self-referential git sources (deploy values pulled from our own repo) "
            "are not upgrade targets."
        ),
        "First-party image versions are published and promoted by CI and Kargo.",
        "Hold the Envoy Gateway and Proxy family while the header regression and paired guard are resolved.",
        "The custom manager owns Polylane cloudflared tag and separate digest together.",
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


def _custom_extract(config: dict, filename: str, content: str) -> list[dict]:
    extracted = []
    for manager in config["customManagers"]:
        if not any(
            re.search(p[1:-1], filename) for p in manager["managerFilePatterns"]
        ):
            continue
        for pattern in manager["matchStrings"]:
            pattern = re.sub(r"\(\?<([a-zA-Z]+)>", r"(?P<\1>", pattern)
            extracted.extend(
                match.groupdict() for match in re.finditer(pattern, content)
            )
    return extracted


def test_custom_download_patterns_keep_checksums_and_archive_members_in_scope():
    config = json.loads((ROOT / "renovate.json").read_text())
    checksum = "a" * 64
    for member in ("", '        "file": "gh_2.87.3_linux_arm64/bin/gh",\n'):
        content = (
            '"url": "https://github.com/cli/cli/releases/download/v2.87.3/'
            'gh_2.87.3_linux_arm64.tar.gz",\n'
            + member
            + '        "sha256": "'
            + checksum
            + '"'
        )
        deps = _custom_extract(config, "bazel/tools/tools.lock.json", content)
        assert len(deps) == 1
        assert deps[0]["depName"] == "cli/cli"
        assert deps[0]["currentValue"] == "v2.87.3"
        assert deps[0]["currentDigest"] == checksum
        assert not _custom_extract(
            config, "bazel/tools/tools.lock.json", content.replace(checksum, "")
        )
    content = (
        f'amd64_sha256 = "{checksum}",\n'
        '    amd64_url = "https://github.com/block/goose/releases/download/v1.39.0/goose.tar.bz2"'
    )
    deps = _custom_extract(config, "MODULE.bazel", content)
    assert deps[0]["depName"] == "block/goose"
    assert deps[0]["currentDigest"] == checksum
    # No regex may advance a URL without also selecting the checked digest.
    assert not _custom_extract(config, "MODULE.bazel", content.replace(checksum, ""))


def test_two_line_bazelversion_and_module_toolchains_are_extracted():
    config = json.loads((ROOT / "renovate.json").read_text())
    values = _custom_extract(config, ".bazelversion", "buildbuddy-io/5.0.321\n9.1.0\n")
    assert [dep["currentValue"] for dep in values] == ["5.0.321", "9.1.0"]
    values = _custom_extract(
        config,
        "MODULE.bazel",
        'node.toolchain(node_version = "22.22.0")\npnpm.pnpm(pnpm_version = "10.34.5")',
    )
    assert [dep["currentValue"] for dep in values] == ["22.22.0", "10.34.5"]


def test_overlays_are_scanned_and_python_regeneration_is_root_only():
    config = json.loads((ROOT / "renovate.json").read_text())
    for path in (
        "projects/platform/cloudflare-gateway/values.yaml",
        "projects/platform/polylane/values.yaml",
        "projects/platform/renovate/values-gke.yaml",
        "projects/monolith/deploy/values.yaml",
    ):
        assert any(
            re.search(p[1:-1], path)
            for p in config["helm-values"]["managerFilePatterns"]
        )
    assert config["kubernetes"]["managerFilePatterns"]
    tasks = [r for r in config["packageRules"] if "postUpgradeTasks" in r]
    assert len(tasks) == 1
    assert tasks[0]["matchManagers"] == ["pep621"]
    assert tasks[0]["matchFileNames"] == ["pyproject.toml"]
    assert tasks[0]["postUpgradeTasks"]["commands"] == [
        "bazel/tools/format/update-python-requirements.sh"
    ]
    helm = [r for r in config["packageRules"] if r.get("matchManagers") == ["helmv3"]]
    assert helm[0]["postUpdateOptions"] == ["helmUpdateSubChartArchives"]
    workflow = (HERE / "templates" / "cronworkflow.yaml").read_text()
    assert "bump-renovated-chart" not in workflow
    assert "value: jomcgi-org/homelab" in workflow


def test_envoy_family_is_held_but_cloudflared_and_other_upstreams_are_eligible():
    config = json.loads((ROOT / "renovate.json").read_text())
    for name in (
        "envoyproxy/gateway-helm",
        "docker.io/envoyproxy/gateway-helm",
        "envoyproxy/gateway-crds-helm",
        "docker.io/envoyproxy/gateway-crds-helm",
        "envoyproxy/envoy",
        "docker.io/envoyproxy/envoy",
        "envoyproxy/gateway",
    ):
        assert _dependency_is_disabled(config, name)
    for name in ("cloudflare/cloudflared", "docker.io/istio/proxyv2", "node", "pnpm"):
        assert not _dependency_is_disabled(config, name)
    assert _dependency_is_disabled(config, "ghcr.io/jomcgi/homelab/projects/monolith")
    assert not _dependency_is_disabled(
        config, "ghcr.io/upstream/homelab/projects/monolith"
    )


def test_polylane_cloudflared_keeps_the_separate_digest_in_the_update():
    config = json.loads((ROOT / "renovate.json").read_text())
    content = (
        "repository: docker.io/cloudflare/cloudflared\n"
        '      tag: "2026.10.4"\n'
        '      digest: "sha256:' + "a" * 64 + '"\n'
    )
    deps = _custom_extract(config, "projects/platform/polylane/values.yaml", content)
    assert len(deps) == 1
    assert deps[0]["currentValue"] == "2026.10.4"
    assert deps[0]["currentDigest"] == "sha256:" + "a" * 64
    handoff = [
        r
        for r in config["packageRules"]
        if r.get("matchFileNames") == ["projects/platform/polylane/values.yaml"]
    ]
    assert len(handoff) == 1
    assert handoff[0]["matchManagers"] == ["helm-values"]
    assert handoff[0]["enabled"] is False
    assert set(handoff[0]["matchPackageNames"]) == {
        "cloudflare/cloudflared",
        "docker.io/cloudflare/cloudflared",
    }
