"""Verify dependency extraction, safe maintenance and reviewed merge policy."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
from pathlib import Path

from packaging.version import Version
import pytest
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


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


@pytest.mark.parametrize(
    ("update_type", "automerge"),
    [
        ("minor", True),
        ("patch", True),
        ("digest", True),
        ("pin", True),
        ("pinDigest", True),
        ("lockFileMaintenance", True),
        ("major", False),
        ("replacement", False),
        ("bump", False),
        ("rollback", False),
    ],
)
def test_only_non_major_updates_request_native_automerge(update_type, automerge):
    config = json.loads((ROOT / "renovate.json").read_text())
    assert config["automerge"] is False  # Unlisted update types stay manual.
    assert config["platformAutomerge"] is True
    effective = config.copy()
    for rule in config["packageRules"]:
        if update_type in rule.get("matchUpdateTypes", []):
            effective.update(rule)
    assert effective["automerge"] is automerge
    if automerge:
        assert effective["platformAutomerge"] is True
    assert effective["automergeType"] == "pr"
    assert config["automergeStrategy"] == "auto"
    assert config["dependencyDashboardApproval"] is False
    assert config["stopUpdatingLabel"] == "human"

    # Neither repository policy nor a more specific rule can bypass review.
    def check_review(value):
        if isinstance(value, dict):
            assert value.get("autoApprove", False) is False
            assert value.get("ignoreTests", False) is False
            assert value.get("statusCheckNames", ["default"]) is not None
            for nested in value.values():
                check_review(nested)
        elif isinstance(value, list):
            for nested in value:
                check_review(nested)

    check_review(config)
    assert config["minimumReleaseAge"] == "3 days"
    assert config["internalChecksFilter"] == "strict"
    assert config["lockFileMaintenance"]["enabled"] is True


@pytest.mark.parametrize(
    ("group_name", "package_names"),
    [
        (
            "OpenTelemetry Python packages",
            (
                "opentelemetry-api",
                "opentelemetry-sdk",
                "opentelemetry-semantic-conventions",
                "opentelemetry-exporter-otlp",
                "opentelemetry-exporter-otlp-proto-common",
                "opentelemetry-exporter-otlp-proto-grpc",
                "opentelemetry-exporter-otlp-proto-http",
                "opentelemetry-proto",
                "opentelemetry-instrumentation",
                "opentelemetry-instrumentation-asgi",
                "opentelemetry-instrumentation-fastapi",
                "opentelemetry-instrumentation-httpx",
                "opentelemetry-util-http",
                "opentelemetry-instrumentation-future-library",
            ),
        ),
        ("Pydantic Python packages", ("pydantic", "pydantic-core", "pydantic_core")),
    ],
)
def test_lockstep_python_packages_share_a_group(group_name, package_names):
    config = json.loads((ROOT / "renovate.json").read_text())
    rules = [
        rule for rule in config["packageRules"] if rule.get("groupName") == group_name
    ]
    assert len(rules) == 1
    rule = rules[0]
    # Apply across Python managers/manifests and update types. In particular,
    # core and prerelease instrumentation versions need not share a major.
    assert {key for key in rule if key.startswith("match")} == {
        "matchDatasources",
        "matchPackageNames",
    }
    assert rule["matchDatasources"] == ["pypi"]
    assert rule["groupSlug"] == group_name.lower().replace(" ", "-")
    assert rule["separateMajorMinor"] is False
    assert rule["separateMinorPatch"] is False
    # Family PRs inherit the repository release-age filter and the non-major
    # native auto-merge rule, which GitHub routes through the merge queue.
    for inherited in (
        "automerge",
        "platformAutomerge",
        "internalChecksFilter",
        "minimumReleaseAge",
        "prCreation",
    ):
        assert inherited not in rule, inherited
    for name in package_names:
        # Renovate's pip_requirements extractor normalizes packageName, including
        # the pydantic_core spelling pinned in the verifier requirements.
        normalized = re.sub(r"[-_.]+", "-", name).lower()
        assert any(
            fnmatch.fnmatchcase(normalized, pattern)
            for pattern in rule["matchPackageNames"]
        ), name
    for unrelated in (
        "fastapi",
        "pytest",
        "pydantic-settings",
        "pydantic-ai-slim",
        "pydantic-graph",
        "opentelemetry",
    ):
        assert not any(
            fnmatch.fnmatchcase(unrelated, pattern)
            for pattern in rule["matchPackageNames"]
        ), unrelated


def test_security_alerts_preserve_explicit_lockstep_groups():
    config = json.loads((ROOT / "renovate.json").read_text())
    # Renovate 44.14.10 init/vulnerability.ts forces vulnerabilityAlerts over
    # package rules, including a default groupName=null. Use the explicit slug
    # so a security fix shares its family's branch with eligible peer updates.
    # Dependencies without an explicit slug retain their per-package branch.
    assert config["vulnerabilityAlerts"]["groupName"] == (
        "{{#if groupSlug}}{{{groupSlug}}}"
        "{{else}}{{{datasource}}}-{{{depNameSanitized}}}-vulnerability{{/if}}"
    )
    assert config["vulnerabilityAlerts"]["enabled"] is True
    assert config["vulnerabilityAlerts"]["schedule"] == ["at any time"]
    assert config["vulnerabilityAlerts"]["minimumReleaseAge"] == "1 day"
    assert config["vulnerabilityAlerts"]["prCreation"] == "immediate"


def _python_update_config(
    config,
    name,
    *,
    security=False,
    filename="projects/model-bench/requirements-venv.txt",
):
    """Apply matching rules and the forced GitHub vulnerability configuration."""
    inputs = {
        "matchDatasources": "pypi",
        "matchManagers": "pip_requirements",
        "matchPackageNames": re.sub(r"[-_.]+", "-", name).lower(),
        "matchDepNames": name,
        "matchFileNames": filename,
        "matchDepTypes": "",
        "matchUpdateTypes": "minor",
    }
    effective = {
        "ignoreUnstable": True,
        "vulnerabilityFixStrategy": "lowest",
        "isVulnerabilityAlert": False,
        "branchConcurrentLimit": None,
        "commitHourlyLimit": 0,
        **config,
    }
    for rule in config["packageRules"]:
        matchers = {key for key in rule if key.startswith("match")}
        assert matchers <= inputs.keys(), "Extend the policy fixture for new matchers"
        if all(
            any(
                fnmatch.fnmatchcase(inputs[key].casefold(), pattern.casefold())
                for pattern in rule[key]
            )
            for key in matchers
        ):
            effective.update(rule)
    if security:
        # init/vulnerability.ts appends a rule whose force object overrides
        # package rules. A package-scoped strategy would be overwritten here.
        effective.update(
            {"isVulnerabilityAlert": True, **config["vulnerabilityAlerts"]}
        )
    return effective


@pytest.mark.parametrize(
    ("filename", "automerge"),
    [
        ("projects/platform/renovate/values.yaml", False),
        ("projects/platform-factory/renovate/application.yaml", False),
        ("projects/factory-cluster/install-node.sh", False),
        ("MODULE.bazel", False),
        (".github/workflows/ci.yml", False),
        ("buildbuddy.yaml", False),
        ("projects/model-bench/requirements-venv.txt", True),
    ],
)
@pytest.mark.parametrize("security", [False, True])
def test_path_review_policy_applies_after_general_automerge(
    filename, automerge, security
):
    config = json.loads((ROOT / "renovate.json").read_text())
    effective = _python_update_config(
        config, "example", filename=filename, security=security
    )
    assert effective["automerge"] is automerge
    assert effective["minimumReleaseAge"] == ("1 day" if security else "3 days")


def _minimum_age_days(config):
    return {"1 day": 1, "3 days": 3}[config["minimumReleaseAge"]]


def _selected_python_release(config, name, current, releases, *, first_fixed=None):
    """Replay the relevant Renovate 44.14.10 lookup stages for PEP 440 fixtures.

    filter.ts handles ignoreUnstable; index.ts narrows vulnerability releases;
    filter-checks.ts selects the newest candidate passing internal checks.
    This models proposed updates, not pip resolution or a live Renovate run.
    """
    effective = _python_update_config(config, name, security=first_fixed is not None)
    current = Version(current)
    candidates = sorted(
        (Version(version), age)
        for version, age in releases
        if Version(version) > current
    )
    if effective["ignoreUnstable"]:
        candidates = [
            (version, age)
            for version, age in candidates
            if not version.is_prerelease
            or (
                current.is_prerelease
                and (version.major, version.minor, version.micro)
                == (current.major, current.minor, current.micro)
            )
        ]
    if first_fixed is not None:
        candidates = [
            (version, age)
            for version, age in candidates
            if version >= Version(first_fixed)
        ]
        if effective["vulnerabilityFixStrategy"] == "lowest":
            candidates = candidates[:1]
        else:
            assert effective["vulnerabilityFixStrategy"] == "highest"
    assert effective["internalChecksFilter"] == "strict"
    minimum_days = _minimum_age_days(effective)
    candidates = [(version, age) for version, age in candidates if age >= minimum_days]
    # None means only pending releases remain: Renovate marks the update
    # pendingChecks and does not propose it (see the branch test below).
    return str(candidates[-1][0]) if candidates else None


@pytest.mark.parametrize(
    "name",
    [
        "opentelemetry-semantic-conventions",
        "opentelemetry-instrumentation",
        "opentelemetry-instrumentation-fastapi",
        "opentelemetry-instrumentation-httpx",
        "opentelemetry-util-http",
        "opentelemetry-configuration",
    ],
)
def test_next_opentelemetry_beta_minor_is_selected_with_stable_sdk(name):
    config = json.loads((ROOT / "renovate.json").read_text())
    assert (
        _selected_python_release(config, name, "0.66b2", [("0.66b3", 7), ("0.67b0", 4)])
        == "0.67b0"
    )
    for stable_name in (
        "opentelemetry-api",
        "opentelemetry-sdk",
        "opentelemetry-exporter-otlp-proto-grpc",
        "opentelemetry-proto",
    ):
        assert (
            _selected_python_release(
                config, stable_name, "1.45.2", [("1.46.0", 4), ("1.47.0rc1", 4)]
            )
            == "1.46.0"
        )


@pytest.mark.parametrize("family", ["opentelemetry", "pydantic"])
@pytest.mark.parametrize("peer_age", [0, 1, 7])
def test_security_updates_shorten_the_wait_for_only_the_vulnerable_member(
    family, peer_age
):
    config = json.loads((ROOT / "renovate.json").read_text())
    # Synthetic publisher-matched release sets: the first patch fixes the
    # advisory, but a newer release requires a newer exact peer version.
    if family == "opentelemetry":
        releases = {
            "opentelemetry-api": ("1.45.2", "1.45.3", "1.46.0"),
            "opentelemetry-sdk": ("1.45.2", "1.45.3", "1.46.0"),
            "opentelemetry-semantic-conventions": ("0.66b2", "0.66b3", "0.67b0"),
        }
    else:
        releases = {
            "pydantic": ("2.13.4", "2.13.5", "2.14.0"),
            "pydantic_core": ("2.46.4", "2.46.5", "2.47.0"),
        }
    for vulnerable in releases:
        for name, (current, fixed, latest) in releases.items():
            selected = _selected_python_release(
                config,
                name,
                current,
                [(fixed, 7), (latest, peer_age)],
                first_fixed=fixed if name == vulnerable else None,
            )
            effective = _python_update_config(config, name, security=name == vulnerable)
            if name == vulnerable:
                assert effective["minimumReleaseAge"] == "1 day"
                assert selected == (latest if peer_age >= 1 else fixed)
            else:
                # Aged peers match that fix. A pending peer keeps its newest
                # aged release until the first scan after the cooldown.
                assert effective["minimumReleaseAge"] == "3 days"
                assert selected == (latest if peer_age >= 3 else fixed)


def _group_limit(upgrades, name):
    # Renovate 44.14.10 workers/global/limits.ts calcLimit: inspect every
    # upgrade, inherit the PR limit for branches, and let any zero win.
    limits = []
    for upgrade in upgrades:
        limit = upgrade[name]
        if name == "branchConcurrentLimit" and limit is None:
            limit = upgrade["prConcurrentLimit"]
        if limit is None or limit == 0:
            return 0
        limits.append(limit)
    return min(limits)


@pytest.mark.parametrize(
    ("members", "vulnerable"),
    [
        (("pydantic", "pydantic_core"), "pydantic_core"),
        (("pydantic", "pydantic-core"), "pydantic-core"),
        (
            (
                "opentelemetry-api",
                "opentelemetry-sdk",
                "opentelemetry-semantic-conventions",
            ),
            "opentelemetry-sdk",
        ),
    ],
)
@pytest.mark.parametrize("security", [False, True])
def test_lockstep_branch_schedule_and_limits_with_unaffected_first_member(
    members, vulnerable, security
):
    config = json.loads((ROOT / "renovate.json").read_text())
    # Exercise hourly commit limits too, even though production defaults to 0.
    config["commitHourlyLimit"] = 1
    upgrades = [
        {
            **_python_update_config(
                config, name, security=security and name == vulnerable
            ),
            "depName": name,
        }
        for name in members
    ]
    # updates/generate.ts sorts by raw depName and copies the first upgrade's
    # config to the branch. A security flag on a later member is not propagated.
    upgrades.sort(key=lambda upgrade: upgrade["depName"])
    branch = upgrades[0]
    assert branch["depName"] != vulnerable
    assert branch["isVulnerabilityAlert"] is False
    # This schedule also admits a Tuesday scan; an ordinary first member must
    # not put the group's security fix back behind Monday.
    assert branch["schedule"] == ["at any time"]
    assert branch["internalChecksFilter"] == "strict"
    assert branch["minimumReleaseAge"] == "3 days"

    saturated_counts = {
        "prConcurrentLimit": config["prConcurrentLimit"],
        "prHourlyLimit": config["prHourlyLimit"],
        "branchConcurrentLimit": config["prConcurrentLimit"],
        "commitHourlyLimit": config["commitHourlyLimit"],
    }
    for name, count in saturated_counts.items():
        limit = _group_limit(upgrades, name)
        assert limit == (0 if security else count)
        # A grouped alert must remain eligible when ordinary quotas are full.
        assert bool(limit and count >= limit) is (not security)


@pytest.mark.parametrize(
    "members",
    [
        ("pydantic", "pydantic_core"),
        ("pydantic", "pydantic-core"),
        ("opentelemetry-api", "opentelemetry-sdk"),
        ("opentelemetry-api", "opentelemetry-semantic-conventions"),
    ],
)
@pytest.mark.parametrize(
    ("ages", "vulnerable_index", "proposed"),
    [
        ((1, 7), None, (False, True)),
        ((7, 1), None, (True, False)),
        ((None, 7), None, (False, True)),
        ((7, None), None, (True, False)),
        ((3, 3), None, (True, True)),
        ((1, 1), None, (False, False)),
        ((0, 7), 0, (False, True)),
        ((None, 7), 0, (False, True)),
        ((1, 7), 0, (True, True)),
        ((7, 1), 1, (True, True)),
        ((1, 1), 0, (True, False)),
        ((1, 1), 1, (False, True)),
    ],
)
def test_lockstep_prs_exclude_pending_releases_and_use_the_merge_queue(
    members, ages, vulnerable_index, proposed
):
    config = json.loads((ROOT / "renovate.json").read_text())
    upgrades = [
        _python_update_config(config, name, security=index == vulnerable_index)
        for index, name in enumerate(members)
    ]
    for upgrade in upgrades:
        # Same path as every other non-major update: native auto-merge, which
        # GitHub sends through the required checks and merge queue.
        assert upgrade["automerge"] is True
        assert upgrade["automergeType"] == "pr"
        assert upgrade["platformAutomerge"] is True
        assert upgrade.get("ignoreTests", False) is False
        assert upgrade.get("minimumReleaseAgeBehaviour", "timestamp-required") == (
            "timestamp-required"
        )

    # Renovate 44.14.10 lookup/filter-checks.ts: with strict filtering, a member
    # whose new releases are all younger than minimumReleaseAge, or lack a
    # timestamp, gets pendingChecks. updates/generate.ts drops pending members
    # from a branch with other updates; update/branch/index.ts creates no
    # branch when every member is pending. No PR carries a pending release.
    pending = [
        upgrade["internalChecksFilter"] == "strict"
        and (age is None or age < _minimum_age_days(upgrade))
        for upgrade, age in zip(upgrades, ages)
    ]
    assert tuple(not p and not all(pending) for p in pending) == proposed


def test_unrelated_updates_keep_strict_age_and_stability_filtering():
    config = json.loads((ROOT / "renovate.json").read_text())
    effective = _python_update_config(config, "fastapi")
    assert effective["schedule"] == ["before 6am on monday"]
    assert effective["prConcurrentLimit"] == 5
    assert effective["prHourlyLimit"] == 2
    assert effective["platformAutomerge"] is True
    assert (
        _selected_python_release(
            config,
            "fastapi",
            "0.138.2",
            [("0.139.0", 7), ("0.140.0", 1), ("0.141.0rc1", 7)],
        )
        == "0.139.0"
    )


def test_compatibility_holds_override_non_major_automerge():
    config = json.loads((ROOT / "renovate.json").read_text())
    enabled_rule = next(
        i
        for i, rule in enumerate(config["packageRules"])
        if rule.get("automerge") is True
    )
    held_packages = set()
    for index, rule in enumerate(config["packageRules"]):
        if rule.get("dependencyDashboardApproval"):
            assert index > enabled_rule
            assert rule["automerge"] is False
            assert rule["platformAutomerge"] is False
            held_packages.update(rule["matchPackageNames"])
    assert held_packages == {
        "firecracker-microvm/firecracker",
        "kata-containers/kata-containers",
        "k3s-io/k3s",
        "weave-hand/loom",
        "yannh/kubernetes-json-schema",
    }


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


def test_all_lock_jobs_request_platform_merge_without_approving():
    docs = _render("values.yaml", "values-gke.yaml")
    data = next(
        d["data"]
        for d in docs
        if d.get("kind") == "ConfigMap" and "maintain.sh" in d.get("data", {})
    )
    script = data["maintain.sh"]
    publisher = data["pull-request.mjs"]
    assert "for mode in apko python atlas helm op claude" in script
    assert "//bazel/requirements:runtime -- --upgrade --no-build" in script
    assert "//bazel/requirements:requirements.all -- --upgrade --no-build" in script
    assert "enablePullRequestAutoMerge" in publisher
    assert "mergeMethod: REBASE" in publisher
    assert "approvePullRequest" not in publisher
    assert "pull.draft" in publisher and "pull.assignees.length" in publisher
    assert 'pull.user.type !== "Bot"' in publisher
    assert "hold|blocked|needs-human|do-not-merge|manual|agent-skip" in publisher
    assert 'labels: ["dependencies"]' in publisher
    assert _cron_suspensions(docs) == {
        "renovate": True,
        "renovate-apko-lock-maintenance": True,
    }


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


@pytest.mark.parametrize(
    "values_files",
    [
        ("values.yaml",),
        ("values.yaml", "values-gke.yaml"),
        ("values.yaml", "values-factory.yaml"),
    ],
)
def test_overlays_are_scanned_and_python_regeneration_is_root_only(values_files):
    config = json.loads((ROOT / "renovate.json").read_text())
    for path in (
        "projects/platform/cloudflare-gateway/values.yaml",
        "projects/platform/polylane/values.yaml",
        "projects/platform/renovate/values-gke.yaml",
        "projects/platform/renovate/values-factory.yaml",
        "projects/monolith/deploy/values.yaml",
    ):
        assert any(
            re.search(p[1:-1], path)
            for p in config["helm-values"]["managerFilePatterns"]
        )
    assert config["kubernetes"]["managerFilePatterns"]
    tasks = [r for r in config["packageRules"] if "postUpgradeTasks" in r]
    assert len(tasks) == 1
    assert tasks[0]["matchManagers"] == ["pep621", "pip_requirements"]
    assert tasks[0]["matchFileNames"] == [
        "pyproject.toml",
        "bazel/requirements/overrides.txt",
    ]
    assert tasks[0]["postUpgradeTasks"]["commands"] == [
        "bazel/tools/format/update-python-requirements.sh"
    ]
    assert (
        "bazel/tools/python/gazelle_python.yaml"
        in tasks[0]["postUpgradeTasks"]["fileFilters"]
    )
    helm = [r for r in config["packageRules"] if r.get("matchManagers") == ["helmv3"]]
    assert helm[0]["postUpdateOptions"] == ["helmUpdateSubChartArchives"]
    # Check the effective runner after Helm resolves the shared container
    # template, for both the hub workflows and the factory Job.
    workload = next(
        doc
        for doc in _render(*values_files)
        if doc["kind"] in {"CronWorkflow", "CronJob"}
        and doc["metadata"]["name"] == "renovate"
    )
    assert "bump-renovated-chart" not in json.dumps(workload)
    if workload["kind"] == "CronJob":
        container = workload["spec"]["jobTemplate"]["spec"]["template"]["spec"][
            "containers"
        ][0]
    else:
        container = workload["spec"]["workflowSpec"]["templates"][0]["container"]
    env = {entry["name"]: entry for entry in container["env"]}
    assert env["RENOVATE_REPOSITORIES"]["value"] == "jomcgi-org/homelab"
    assert json.loads(env["RENOVATE_ALLOWED_COMMANDS"]["value"]) == [
        r"^bazel/tools/format/update-python-requirements\.sh$"
    ]


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


def test_factory_digest_pins_outside_helm_images_are_extracted_with_their_digest():
    config = json.loads((ROOT / "renovate.json").read_text())
    digest = "sha256:" + "b" * 64
    onepassword = (
        "connect:\n"
        "  connect:\n"
        "    # Each container's tag, pinned by digest.\n"
        "    api:\n"
        f'      version: "1.8.1@{digest}"\n'
        "    sync:\n"
        f'      version: "1.8.2@{digest}"\n'
        "  operator:\n"
        "    # The operator's chart tag.\n"
        f'    version: "1.9.1@{digest}"\n'
    )
    managers = {
        manager["depNameTemplate"]: manager
        for manager in config["customManagers"]
        if manager.get("depNameTemplate", "").startswith("1password/")
    }
    assert set(managers) == {
        "1password/connect-api",
        "1password/connect-sync",
        "1password/onepassword-operator",
    }
    versions = {}
    for name, manager in managers.items():
        assert manager["datasourceTemplate"] == "docker"
        (pattern,) = manager["matchStrings"]
        pattern = re.sub(r"\(\?<([a-zA-Z]+)>", r"(?P<\1>", pattern)
        (match,) = re.finditer(pattern, onepassword)
        assert match["currentDigest"] == digest
        versions[name] = match["currentValue"]
    assert versions == {
        "1password/connect-api": "1.8.1",
        "1password/connect-sync": "1.8.2",
        "1password/onepassword-operator": "1.9.1",
    }
    cluster = (
        "spec:\n"
        "  imageName: ghcr.io/cloudnative-pg/postgresql:"
        f"18.4-system-trixie@{digest}\n"
    )
    deps = _custom_extract(
        config, "projects/platform-factory/x/resources/cluster.yaml", cluster
    )
    assert deps == [
        {
            "depName": "ghcr.io/cloudnative-pg/postgresql",
            "currentValue": "18.4-system-trixie",
            "currentDigest": digest,
        }
    ]
    pin_rule = next(r for r in config["packageRules"] if r.get("pinDigests") is True)
    assert "ghcr.io/cloudnative-pg/postgresql" in pin_rule["matchPackageNames"]
    assert "1password/connect-api" in pin_rule["matchPackageNames"]


def _pins_module():
    import importlib.machinery
    import importlib.util

    # This is a Helm ConfigMap asset, not a standalone Bazel Python binary.
    loader = importlib.machinery.SourceFileLoader(
        "custom_pins", str(HERE / "files" / "custom-pins.py.tpl")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_custom_pin_updates_are_atomic_and_keep_every_platform():
    import hashlib
    from datetime import datetime, timezone

    import pytest

    pins = _pins_module()
    original = {
        "helm": {
            "binaries": [
                {
                    "url": f"https://get.helm.sh/helm-v3.16.4-{platform}.tar.gz",
                    "sha256": "a" * 64,
                    "file": "helm",
                }
                for platform in (
                    "linux-amd64",
                    "linux-arm64",
                    "darwin-arm64",
                    "darwin-amd64",
                )
            ]
        }
    }
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    downloads = []

    def read(url, *args):
        if "api.github.com" in url:
            return json.dumps(
                [
                    {
                        "tag_name": "v4.0.0",
                        "published_at": "2026-10-20T00:00:00Z",
                        "draft": False,
                        "prerelease": False,
                    },
                    {
                        "tag_name": "v5.0.0",
                        "published_at": "2026-10-26T00:00:00Z",
                        "draft": False,
                        "prerelease": False,
                    },
                    {
                        "tag_name": "v6.0.0",
                        "published_at": "2026-10-20T00:00:00Z",
                        "draft": True,
                        "prerelease": False,
                    },
                ]
            ).encode()
        assert "v4.0.0" in url
        if url.endswith(".sha256sum"):
            return hashlib.sha256(b"archive").hexdigest().encode()
        downloads.append(url)
        return b"archive"

    result = pins.update(original, "helm", now=now, read=read)
    assert len(downloads) == 4
    assert all(
        "v4.0.0" in row["url"]
        and row["sha256"] == hashlib.sha256(b"archive").hexdigest()
        for row in result["helm"]["binaries"]
    )
    assert all("v3.16.4" in row["url"] for row in original["helm"]["binaries"])
    before = json.dumps(original)

    def corrupt(url, *args):
        if "darwin-arm64" in url and not url.endswith(".sha256sum"):
            return b"tampered"
        return read(url, *args)

    with pytest.raises(ValueError, match="checksum mismatch"):
        pins.update(original, "helm", now=now, read=corrupt)
    assert json.dumps(original) == before


def test_custom_pin_origins_and_npm_integrity_fail_closed():
    import base64
    import hashlib

    import pytest

    pins = _pins_module()
    for url in (
        "http://get.helm.sh/file",
        "https://get.helm.sh.evil/file",
        "https://user:password@get.helm.sh/file",
        "https://get.helm.sh:123/file",
        "file:///etc/passwd",
    ):
        with pytest.raises(ValueError):
            pins.allowed(url)
    package = "@anthropic-ai/claude-code-linux-arm64"
    old = {
        "url": f"https://registry.npmjs.org/{package}/-/claude-code-linux-arm64-2.1.222.tgz",
        "sha256": "a" * 64,
    }
    new_url = old["url"].replace("2.1.222", "3.0.0")
    metadata = {
        "name": package,
        "version": "3.0.0",
        "dist": {
            "tarball": new_url,
            "integrity": "sha512-"
            + base64.b64encode(hashlib.sha512(b"archive").digest()).decode(),
        },
    }

    def read(url, *args):
        return b"archive" if url == new_url else json.dumps(metadata).encode()

    assert pins.replace_binary("claude", old, "3.0.0", read)["url"] == new_url
    metadata["dist"]["integrity"] = "sha512-AAAA"
    with pytest.raises(ValueError, match="integrity mismatch"):
        pins.replace_binary("claude", old, "3.0.0", read)
    metadata["dist"]["tarball"] = "https://evil.example/archive"
    with pytest.raises(ValueError, match="identity mismatch"):
        pins.replace_binary("claude", old, "3.0.0", read)


def test_custom_atlas_and_op_preserve_binary_flavour_and_skip_downgrades():
    import hashlib
    from datetime import datetime, timezone

    import pytest

    pins = _pins_module()
    archive = b"archive"
    for tool, url in (
        ("atlas", "https://release.ariga.io/atlas/atlas-community-linux-amd64-v1.1.0"),
        (
            "op",
            "https://cache.agilebits.com/dist/1P/op2/pkg/v1.1.0/op_linux_amd64_v1.1.0.zip",
        ),
    ):
        binary = {"url": url, "sha256": "a" * 64, "file": tool}

        def read(target, *args):
            return (
                hashlib.sha256(archive).hexdigest().encode()
                if target.endswith(".sha256")
                else archive
            )

        result = pins.replace_binary(tool, binary, "2.0.0", read)
        assert result["url"] == url.replace("1.1.0", "2.0.0")
        assert result["sha256"] == hashlib.sha256(archive).hexdigest()
        assert (
            pins.replace_binary(
                tool, binary, "1.0.0", lambda *_: pytest.fail("downloaded downgrade")
            )
            == binary
        )
    history = b'<h3>2.41.1 <span class="date">released 2026-10-21</span></h3><h3>2.42.0-beta.1 <span>released 2026-10-20</span></h3>'
    assert (
        pins.latest(
            "op", datetime(2026, 10, 27, tzinfo=timezone.utc), lambda _: history
        )
        == "2.41.1"
    )


@pytest.mark.parametrize("generator_exit", [3, 17])
def test_failed_or_held_maintenance_never_pushes_and_other_modes_continue(
    tmp_path, generator_exit
):
    docs = _render("values.yaml", "values-gke.yaml")
    data = next(
        d["data"]
        for d in docs
        if d.get("kind") == "ConfigMap" and "maintain.sh" in d.get("data", {})
    )
    workspace = tmp_path / "checkout"
    (workspace / "projects" / "example").mkdir(parents=True)
    (workspace / "projects" / "example" / "apko.yaml").write_text("contents: {}")
    (workspace / "projects" / "example" / "apko.lock.json").write_text("{}")
    log = tmp_path / "commands.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    commands = {
        "git": 'case "$1 $2" in "diff --quiet"|"ls-remote --exit-code") exit 1;; esac',
        "node": 'if [ "$MAINTENANCE_MODE" = helm ]; then exit 3; fi',
        "bazel": f'if [ "$MAINTENANCE_MODE" = apko ]; then exit {generator_exit}; fi',
        "python3": "true",
    }
    for name, body in commands.items():
        stub = bin_dir / name
        stub.write_text(
            '#!/bin/bash\nprintf "%s %s %s\\n" "${MAINTENANCE_MODE:-setup}" "'
            + name
            + '" "$*" >> "$MAINTENANCE_LOG"\n'
            + body
            + "\n"
        )
        stub.chmod(0o755)
    script = tmp_path / "maintain.sh"
    script.write_text(
        data["maintain.sh"]
        .replace("/tmp/dependency-maintenance", str(workspace))
        .replace("/tmp/renovate-git-askpass", str(tmp_path / "askpass"))
    )
    result = subprocess.run(
        ["bash", str(script)],
        env={
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "MAINTENANCE_LOG": str(log),
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 1, result.stderr
    output = log.read_text()
    assert "apko bazel run" in output
    assert "apko git commit" not in output and "apko git push" not in output
    assert "helm git commit" not in output and "helm git push" not in output
    for mode in ("python", "atlas", "op", "claude"):
        assert f"{mode} git push" in output
        assert f"{mode} node /opt/renovate-apko/pull-request.mjs publish" in output


def test_bot_pr_with_human_ownership_is_never_rewritten_or_armed():
    # Execute the real publisher with a fake GitHub transport, including the
    # explicit human handoff label. No approval or network request is real.
    runner = r"""
      import { readFileSync } from "node:fs";
      import assert from "node:assert/strict";
      const source = readFileSync(process.argv[1], "utf8");
      let index = 0;
      const base = {number: 1, node_id: "PR_example", draft: false,
        assignees: [], labels: [], user: {type: "Bot"}, html_url: "https://example.test/pr"};
      for (const scenario of ["new", "existing", "armed", "queued", "immediate-queue", "draft", "assigned", "held", "human-label", "human-author", "refused"]) {
        const calls = [];
        const pull = {...base};
        if (scenario === "draft") pull.draft = true;
        if (scenario === "assigned") pull.assignees = [{}];
        if (scenario === "held") pull.labels = [{name: "needs-human"}];
        if (scenario === "human-label") pull.labels = [{name: "human"}];
        if (scenario === "human-author") pull.user = {type: "User"};
        if (scenario === "armed") pull.auto_merge = {};
        process.env.MAINTENANCE_MODE = "apko";
        process.env.MAINTENANCE_AUTOMERGE_ENABLED = "true";
        process.env.RENOVATE_TOKEN = "mock-token";
        process.argv[2] = "publish";
        globalThis.fetch = async (url, options) => {
          calls.push(url);
          let result;
          if (url.includes("/pulls?")) result = scenario === "new" ? [] : [pull];
          else if (url.endsWith("/pulls")) result = pull;
          else if (url.endsWith("/labels")) result = [];
          else if (url.endsWith("/graphql")) {
            const mutation = JSON.parse(options.body).query.startsWith("mutation");
            if (!mutation) result = {data: {node: {autoMergeRequest: null, mergeQueueEntry: scenario === "queued" ? {id: "queue-entry"} : null}}};
            else result = scenario === "refused" ? {errors: [{message: "denied"}]} : {data: {enablePullRequestAutoMerge: {pullRequest: {
              autoMergeRequest: scenario === "immediate-queue" ? null : {enabledAt: "now"},
              mergeQueueEntry: scenario === "immediate-queue" ? {id: "queue-entry"} : null,
            }}}};
          }
          else throw Error("Unexpected network request");
          return {ok: true, status: 200, json: async () => result};
        };
        const exit = process.exit;
        process.exit = code => {throw Object.assign(new Error("exit"), {code})};
        let failure;
        try {await import("data:text/javascript;base64," + Buffer.from(source).toString("base64") + "#" + index++)}
        catch (error) {failure = error}
        finally {process.exit = exit}
        if (["draft", "assigned", "held", "human-label", "human-author"].includes(scenario)) {
          assert.equal(failure?.code, 3, scenario);
          assert.equal(calls.length, 1, scenario);
        } else if (scenario === "refused") {
          assert.match(failure.message, /failed/);
          assert.equal(calls.filter(url => url.endsWith("/graphql")).length, 2);
        } else {
          assert.equal(failure, undefined, scenario);
          assert.equal(calls.some(url => url.endsWith("/graphql")), scenario !== "armed");
        }
        assert.ok(!calls.some(url => url.includes("/reviews") || url.includes("/merge")));
      }
    """
    result = subprocess.run(
        [
            os.environ.get("NODE_BIN", "node"),
            "--input-type=module",
            "-e",
            runner,
            str(HERE / "files" / "pull-request.mjs"),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_app_token_minting_verifies_identity_and_never_logs_credentials(tmp_path):
    # Exercise the shipped init script with a real RSA key and mocked GitHub
    # responses. No App credentials or network access are needed.
    runner = r"""
      import assert from "node:assert/strict";
      import {generateKeyPairSync, verify} from "node:crypto";
      import {existsSync, mkdirSync, readFileSync, statSync, writeFileSync} from "node:fs";
      import {join} from "node:path";
      const source = readFileSync(process.argv[1], "utf8");
      const root = process.argv[2];
      const username = "jomcgi-factory-renovate[bot]";
      const {privateKey, publicKey} = generateKeyPairSync("rsa", {modulusLength: 2048});
      const pem = privateKey.export({type: "pkcs1", format: "pem"});
      const token = "ghs_mock_installation_token";
      const now = 1_800_000_000_000;
      Date.now = () => now;
      let index = 0;
      for (const scenario of ["success", "bad-key", "bad-app-id", "bad-installation-id",
        "rejected", "network", "bad-json", "bad-token", "expired-token",
        "bot-rejected", "wrong-bot", "wrong-id", "wrong-type"]) {
        const dir = join(root, scenario);
        mkdirSync(dir);
        const output = join(dir, "output");
        mkdirSync(output);
        writeFileSync(join(dir, "private-key"), scenario === "bad-key" ? "secret-invalid-key" : pem);
        writeFileSync(join(dir, "app-id"), scenario === "bad-app-id" ? "123/invalid" : "123\n");
        writeFileSync(join(dir, "installation-id"), scenario === "bad-installation-id" ? "456/invalid" : "456\n");
        process.env.GITHUB_APP_CREDENTIALS_DIR = dir;
        process.env.RENOVATE_TOKEN_DIR = output;
        process.env.RENOVATE_USERNAME = username;
        process.exitCode = 0;
        const calls = [];
        globalThis.fetch = async (url, options) => {
          calls.push({url, options});
          if (calls.length === 1) {
            if (scenario === "network") throw new Error(token + pem);
            return {
              status: scenario === "rejected" ? 401 : 201,
              json: async () => {
                if (scenario === "bad-json") throw new Error(token + pem);
                return {
                  token: scenario === "bad-token" ? "" : token,
                  expires_at: new Date(now + (scenario === "expired-token" ? -1 : 3_600_000)).toISOString(),
                };
              },
            };
          }
          return {
            status: scenario === "bot-rejected" ? 404 : 200,
            json: async () => ({
              login: scenario === "wrong-bot" ? "someone-else[bot]" : username,
              id: scenario === "wrong-id" ? "12345" : 12345,
              type: scenario === "wrong-type" ? "User" : "Bot",
            }),
          };
        };
        const errors = [];
        const originalError = console.error;
        console.error = (...args) => errors.push(args.join(" "));
        try {
          await import("data:text/javascript;base64," + Buffer.from(source).toString("base64") + "#" + index++);
        } finally {
          console.error = originalError;
        }
        if (["bad-key", "bad-app-id", "bad-installation-id"].includes(scenario)) {
          assert.equal(calls.length, 0, scenario);
        } else {
          const {url, options} = calls[0];
          assert.equal(url, "https://api.github.com/app/installations/456/access_tokens");
          assert.equal(options.method, "POST");
          assert.deepEqual(JSON.parse(options.body), {repositories: ["homelab"]});
          const jwt = options.headers.Authorization.replace(/^Bearer /, "");
          const [header, claims, signature] = jwt.split(".");
          assert.deepEqual(JSON.parse(Buffer.from(header, "base64url")), {alg: "RS256", typ: "JWT"});
          assert.deepEqual(JSON.parse(Buffer.from(claims, "base64url")), {
            iat: now / 1000 - 60, exp: now / 1000 + 540, iss: "123",
          });
          assert.ok(verify("RSA-SHA256", Buffer.from(header + "." + claims), publicKey, Buffer.from(signature, "base64url")));
          for (const {options} of calls) {
            assert.equal(options.redirect, "error");
            assert.ok(options.signal instanceof AbortSignal);
            assert.equal(options.headers["X-GitHub-Api-Version"], "2022-11-28");
          }
          if (calls.length === 2) {
            assert.equal(calls[1].url, "https://api.github.com/users/jomcgi-factory-renovate%5Bbot%5D");
            assert.equal(calls[1].options.headers.Authorization, "Bearer " + token);
          }
        }
        if (scenario === "success") {
          assert.equal(process.exitCode, 0);
          assert.deepEqual(errors, []);
          assert.equal(calls.length, 2);
          assert.equal(readFileSync(join(output, "token"), "utf8"), token);
          assert.equal(readFileSync(join(output, "git-author"), "utf8"),
            username + " <12345+" + username + "@users.noreply.github.com>");
          for (const file of ["token", "git-author"]) {
            assert.equal(statSync(join(output, file)).mode & 0o777, 0o600);
          }
        } else {
          assert.equal(process.exitCode, 1, scenario);
          assert.deepEqual(errors, ["GitHub App authentication failed; check the App item and installation"]);
          assert.ok(!existsSync(join(output, "token")), scenario);
          assert.ok(!existsSync(join(output, "git-author")), scenario);
        }
      }
      process.exitCode = 0;
    """
    result = subprocess.run(
        [
            os.environ.get("NODE_BIN", "node"),
            "--input-type=module",
            "-e",
            runner,
            str(HERE / "files/github-app-token.mjs"),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == result.stderr == ""


def test_native_uv_artifacts_cannot_execute_source_build_backends():
    docs = _render("values.yaml", "values-gke.yaml")
    workflow = next(
        d
        for d in docs
        if d.get("kind") == "CronWorkflow" and d["metadata"]["name"] == "renovate"
    )
    env = workflow["spec"]["workflowSpec"]["templates"][0]["container"]["env"]
    custom = next(
        row["value"] for row in env if row["name"] == "RENOVATE_CUSTOM_ENV_VARIABLES"
    )
    assert json.loads(custom) == {"UV_NO_BUILD": "true"}


def test_maintenance_automerge_requires_an_explicit_activation_flag():
    # Exercise the rendered publisher that existing/manual workflows can load.
    # Missing, false and malformed flags must never even query merge state.
    docs = _render("values.yaml", "values-gke.yaml")
    source = next(
        doc["data"]["pull-request.mjs"]
        for doc in docs
        if doc.get("kind") == "ConfigMap" and "pull-request.mjs" in doc.get("data", {})
    )
    runner = r"""
      import assert from "node:assert/strict";
      const source = process.argv[1];
      let index = 0;
      process.env.MAINTENANCE_MODE = "apko";
      process.env.RENOVATE_TOKEN = "mock-token";
      process.argv[2] = "publish";
      for (const flag of [undefined, "false", "FALSE", "TRUE", "1", ""]) {
        if (flag === undefined) delete process.env.MAINTENANCE_AUTOMERGE_ENABLED;
        else process.env.MAINTENANCE_AUTOMERGE_ENABLED = flag;
        for (const scenario of ["new", "existing", "armed", "queued"]) {
          const calls = [];
          const pull = {number: 1, node_id: "PR_example", draft: false,
            assignees: [], labels: [], user: {type: "Bot"}, html_url: "https://example.test/pr",
            auto_merge: scenario === "armed" ? {} : null};
          globalThis.fetch = async (url, options) => {
            calls.push(url);
            let result;
            if (url.includes("/pulls?")) result = scenario === "new" ? [] : [pull];
            else if (url.endsWith("/pulls")) result = pull;
            else if (url.endsWith("/labels")) result = [];
            else throw Error("Unexpected merge request while disabled: " + url);
            return {ok: true, status: 200, json: async () => result};
          };
          await import("data:text/javascript;base64," + Buffer.from(source).toString("base64") + "#" + index++);
          assert.equal(calls.length, scenario === "new" ? 3 : 2);
          assert.ok(!calls.some(url => /graphql|reviews|merge/.test(url)));
        }
      }
    """
    result = subprocess.run(
        [
            os.environ.get("NODE_BIN", "node"),
            "--input-type=module",
            "-e",
            runner,
            source,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_default_and_hub_workflows_cannot_enable_maintenance_automerge(tmp_path):
    for values in (("values.yaml",), ("values.yaml", "values-gke.yaml")):
        docs = _render(*values)
        workflow = next(
            doc
            for doc in docs
            if doc.get("kind") == "CronWorkflow"
            and doc["metadata"]["name"] == "renovate-apko-lock-maintenance"
        )
        env = workflow["spec"]["workflowSpec"]["templates"][0]["container"]["env"]
        assert [
            row.get("value")
            for row in env
            if row["name"] == "MAINTENANCE_AUTOMERGE_ENABLED"
        ] == ["false"]

    # Prove the documented owner opt-in reaches the actual consumer, rather
    # than introducing a flag that is permanently inert or shadowed by defaults.
    activation = tmp_path / "activation.yaml"
    activation.write_text("apkoLockMaintenance:\n  automerge: true\n")
    docs = _render("values.yaml", "values-gke.yaml", str(activation))
    workflow = next(
        doc
        for doc in docs
        if doc.get("kind") == "CronWorkflow"
        and doc["metadata"]["name"] == "renovate-apko-lock-maintenance"
    )
    env = workflow["spec"]["workflowSpec"]["templates"][0]["container"]["env"]
    assert [
        row.get("value")
        for row in env
        if row["name"] == "MAINTENANCE_AUTOMERGE_ENABLED"
    ] == ["true"]
    assert workflow["spec"]["suspend"] is True
