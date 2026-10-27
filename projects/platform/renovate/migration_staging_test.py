"""Pin the staged, single-writer Renovate migration contract."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
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
    assert tasks[0]["matchManagers"] == ["pep621", "pip_requirements"]
    assert tasks[0]["matchFileNames"] == [
        "pyproject.toml",
        "bazel/requirements/overrides.txt",
    ]
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


def _pins_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "custom_pins", HERE / "files" / "custom-pins.py"
    )
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
