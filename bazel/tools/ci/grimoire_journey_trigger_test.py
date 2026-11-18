"""Tests for the Grimoire journey trigger (HOME-223)."""

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile

import grimoire_journey_trigger as trigger

BASE = """\
lockfileVersion: '9.0'

settings:
  autoInstallPeers: true

overrides:
  rollup: 4.68.0

importers:

  .:
    devDependencies:
      happy-dom:
        specifier: 21.0.4
        version: 21.0.4

  projects/design-system:
    dependencies:
      tokens:
        specifier: 1.0.0
        version: 1.0.0

  projects/jomcgi-site/web:
    dependencies:
      react:
        specifier: 19.3.1
        version: 19.3.1

  projects/monolith/frontend:
    dependencies:
      '@homelab/design-system':
        specifier: workspace:*
        version: link:../../design-system
      svelte:
        specifier: 5.1.0
        version: 5.1.0

packages:

  react@19.3.1:
    resolution: {integrity: sha512-react}

  svelte@5.1.0:
    resolution: {integrity: sha512-svelte}

  tokens@1.0.0:
    resolution: {integrity: sha512-tokens}

snapshots:

  react@19.3.1: {}

  svelte@5.1.0:
    dependencies:
      acorn: 8.0.0

  tokens@1.0.0: {}
"""


def _site_adds_playwright(text: str) -> str:
    text = text.replace(
        "      react:\n        specifier: 19.3.1\n        version: 19.3.1\n",
        "      react:\n        specifier: 19.3.1\n        version: 19.3.1\n"
        "      playwright:\n        specifier: 1.50.0\n        version: 1.50.0\n",
    )
    text = text.replace(
        "  react@19.3.1:\n    resolution: {integrity: sha512-react}\n",
        "  playwright@1.50.0:\n    resolution: {integrity: sha512-pw}\n\n"
        "  react@19.3.1:\n    resolution: {integrity: sha512-react}\n",
    )
    return text.replace(
        "  react@19.3.1: {}\n", "  playwright@1.50.0: {}\n\n  react@19.3.1: {}\n"
    )


def test_site_only_lockfile_addition_is_skipped():
    run, reason = trigger.decide(["pnpm-lock.yaml"], BASE, _site_adds_playwright(BASE))
    assert not run, reason


def test_frontend_importer_change_runs():
    head = BASE.replace(
        "        specifier: 5.1.0\n        version: 5.1.0",
        "        specifier: 5.2.0\n        version: 5.2.0",
    )
    run, reason = trigger.decide(["pnpm-lock.yaml"], BASE, head)
    assert run and "projects/monolith/frontend" in reason


def test_linked_importer_change_runs():
    head = BASE.replace(
        "        specifier: 1.0.0\n        version: 1.0.0",
        "        specifier: 1.1.0\n        version: 1.1.0",
    )
    run, reason = trigger.decide(["pnpm-lock.yaml"], BASE, head)
    assert run and "projects/design-system" in reason


def test_root_importer_change_runs():
    head = BASE.replace("version: 21.0.4", "version: 21.0.5")
    run, reason = trigger.decide(["pnpm-lock.yaml"], BASE, head)
    assert run and "'.'" in reason


def test_changed_package_entry_runs():
    head = BASE.replace("acorn: 8.0.0", "acorn: 8.1.0")
    run, reason = trigger.decide(["pnpm-lock.yaml"], BASE, head)
    assert run and "svelte@5.1.0" in reason


def test_removed_package_entry_runs():
    head = BASE.replace(
        "  tokens@1.0.0:\n    resolution: {integrity: sha512-tokens}\n\n", ""
    )
    run, reason = trigger.decide(["pnpm-lock.yaml"], BASE, head)
    assert run and "tokens@1.0.0" in reason


def test_override_change_runs():
    head = BASE.replace("rollup: 4.68.0", "rollup: 4.69.0")
    run, reason = trigger.decide(["pnpm-lock.yaml"], BASE, head)
    assert run and "overrides" in reason


def test_grimoire_path_change_runs():
    run, reason = trigger.decide(["projects/monolith/grimoire/sheets.py"], None, None)
    assert run and "projects/monolith/grimoire/sheets.py" in reason


def test_buildbuddy_change_runs():
    run, _ = trigger.decide(["buildbuddy.yaml"], None, None)
    assert run


def test_unrelated_change_is_skipped():
    run, _ = trigger.decide([], None, None)
    assert not run


def test_lockfile_created_runs():
    run, _ = trigger.decide(["pnpm-lock.yaml"], None, BASE)
    assert run


def test_link_resolution_follows_relative_paths():
    importers = trigger.split_entries(trigger.split_sections(BASE)["importers"])
    assert trigger.watched_importers(importers) == {
        ".",
        "projects/monolith/frontend",
        "projects/design-system",
    }


def test_unusual_path_names_still_trigger():
    raw = "projects/monolith/web/grimoire/caf\u00e9.ts\0pnpm-lock.yaml\0"
    changed = trigger.changed_paths(raw)
    assert changed == ["projects/monolith/web/grimoire/caf\u00e9.ts", "pnpm-lock.yaml"]
    run, reason = trigger.decide(changed, BASE, BASE)
    assert run and "caf\u00e9.ts" in reason


def _buildbuddy() -> str:
    for root in (os.environ.get("RUNFILES_DIR"), os.environ.get("TEST_SRCDIR")):
        if root and (pathlib.Path(root) / "_main/buildbuddy.yaml").exists():
            return (pathlib.Path(root) / "_main/buildbuddy.yaml").read_text()
    return (pathlib.Path(__file__).resolve().parents[3] / "buildbuddy.yaml").read_text()


def _gate() -> str:
    lines = _buildbuddy().splitlines()
    start = next(i for i, l in enumerate(lines) if "if ! grimoire_journeys=" in l)
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "esac")
    return "\n".join(l.strip() for l in lines[start : end + 1])


def _gate_runs_matrix(output: str, code: int) -> bool:
    with tempfile.TemporaryDirectory() as root:
        work = pathlib.Path(root)
        (work / "bazel/tools/ci").mkdir(parents=True)
        (work / "bazel/tools/ci/grimoire_journey_trigger.py").write_text(
            f"import sys\nsys.stdout.write({output!r})\nsys.exit({code})\n"
        )
        runner = work / "projects/monolith/dev/grimoire/ci-runner.sh"
        runner.parent.mkdir(parents=True)
        runner.write_text("#!/usr/bin/env bash\ntouch ran\n")
        runner.chmod(0o755)
        subprocess.run(
            ["bash", "-euo", "pipefail", "-c", f"base_ref=origin/main\n{_gate()}"],
            cwd=work,
            check=True,
            capture_output=True,
        )
        return (work / "ran").exists()


def test_gate_skips_only_on_one_successful_skip_line():
    assert not _gate_runs_matrix("skip (no Grimoire input changed)\n", 0)
    assert _gate_runs_matrix("run (pnpm-lock.yaml changed)\n", 0)
    assert _gate_runs_matrix("skip (no Grimoire input changed)\n", 1)
    assert _gate_runs_matrix("skip (a)\nrun (b)\n", 0)
    assert _gate_runs_matrix("skipped\n", 0)
    assert _gate_runs_matrix("", 0)
