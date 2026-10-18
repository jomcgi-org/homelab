"""Outer orchestration regressions; actual stale-input tests live with the macro."""

import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import tempfile
import unittest

from packaging.markers import default_environment
from packaging.requirements import Requirement


REPO = Path(__file__).resolve().parents[3]


class ManifestWiringTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for path in (
            "bazel/tools/ci/python-manifest.sh",
            "bazel/tools/format/update-python-requirements.sh",
            "bazel/tools/format/update-python-requirements-if-needed.sh",
        ):
            dest = self.root / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / path, dest)
            dest.chmod(0o755)
        bindir = self.root / "bin"
        bindir.mkdir()
        self.log = self.root / "commands"
        for command, body in {
            "bazel": """
printf 'bazel %s\\n' "$*" >>"$COMMAND_LOG"
case "$*" in
  'test //bazel/requirements:'*) exit "${LOCK_STATUS:-0}" ;;
  'test //bazel/tools/python:gazelle_python_manifest.test --config=ci')
    if [[ ! -e "$REPAIRED" ]]; then exit "${INTEGRITY_STATUS:-0}"; fi ;;
  'run //bazel/tools/python:gazelle_python_manifest.update'*) touch "$REPAIRED" ;;
  'test //bazel/tools/python:gazelle_python_manifest.test //bazel/tools/python:manifest_drift_test'*) exit "${CHECK_STATUS:-0}" ;;
esac
""",
            "git": """
printf 'git %s\\n' "$*" >>"$COMMAND_LOG"
if [[ "$1" == diff ]]; then exit "${INPUTS_CHANGED:-0}"; fi
if [[ "$1" == merge-base ]]; then echo branch-base; fi
""",
            "uname": 'echo "${FAKE_OS:-Linux}"',
        }.items():
            dest = bindir / command
            dest.write_text("#!/usr/bin/env bash\nset -eu\n" + body + "\n")
            dest.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "COMMAND_LOG": str(self.log),
            "REPAIRED": str(self.root / "repaired"),
        }
        self.env.pop("BUILD_WORKSPACE_DIRECTORY", None)

    def run_script(self, path, *args, **env):
        result = subprocess.run(
            ["bash", str(self.root / path), *args],
            cwd=self.root,
            env={**self.env, **env},
            text=True,
            capture_output=True,
        )
        return result, self.log.read_text() if self.log.exists() else ""

    def driver(self, mode, **env):
        return self.run_script("bazel/tools/ci/python-manifest.sh", mode, **env)

    def test_integrity_check_is_explicit_and_wheel_comparison_is_scoped(self):
        result, log = self.driver("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("manifest_drift_test --config=ci --nocache_test_results", log)
        self.assertNotIn("deleted_packages", log)
        self.assertNotIn("manifest_generated_test", log)
        result, log = self.driver("check", INPUTS_CHANGED="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("manifest_generated_test", log)

    def test_check_failure_cannot_become_regeneration(self):
        result, log = self.driver("check", CHECK_STATUS="3")
        self.assertEqual(result.returncode, 3)
        self.assertNotIn("bazel run", log)

    def test_update_checks_before_building_wheels(self):
        result, log = self.driver("update")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("bazel run", log)
        result, log = self.driver("update", INTEGRITY_STATUS="3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gazelle_python_manifest.update", log)
        self.assertIn("manifest_generated_test", log)

    def test_analysis_failure_is_not_a_stale_manifest(self):
        result, log = self.driver("update", INTEGRITY_STATUS="1")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("bazel run", log)

    def test_unsupported_or_nested_execution_is_refused(self):
        for env in ({"FAKE_OS": "Darwin"}, {"BUILD_WORKSPACE_DIRECTORY": "/nested"}):
            result, log = self.driver("update", **env)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(log, "")

    def test_full_update_keeps_renovate_lock_only(self):
        result, log = self.run_script("bazel/tools/format/update-python-requirements.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(log.index(":runtime"), log.index(":requirements.all"))
        self.assertNotIn("gazelle_python_manifest", log)
        self.assertNotIn("--config=ci", log)
        self.assertIn("deferred to PR Linux CI", result.stdout)

    def test_runtime_in_sync_still_checks_aggregate(self):
        result, log = self.run_script(
            "bazel/tools/format/update-python-requirements-if-needed.sh",
            INTEGRITY_STATUS="3",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("//bazel/requirements:requirements.all_test", log)
        self.assertNotIn("gazelle_python_manifest", log)
        self.assertIn("git add -u --", log)
        self.assertNotIn("gazelle_python.yaml", log)
        self.assertNotIn("bazel run //bazel/requirements", log)

    def test_aggregate_drift_delegates_full_update(self):
        result, log = self.run_script(
            "bazel/tools/format/update-python-requirements-if-needed.sh", LOCK_STATUS="3"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bazel run //bazel/requirements:requirements.all", log)
        self.assertNotIn("gazelle_python_manifest", log)

    def test_darwin_hook_defers_without_bazel_or_staging(self):
        result, log = self.run_script(
            "bazel/tools/format/update-python-requirements-if-needed.sh", FAKE_OS="Darwin"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("deferred to PR Linux CI", result.stdout)
        self.assertEqual(log, "")

    def test_generated_comparison_uses_merge_base_and_working_tree(self):
        result, log = self.driver("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("git merge-base origin/main HEAD", log)
        self.assertIn("git diff --quiet branch-base -- MODULE.bazel", log)
        self.assertNotIn("branch-base...HEAD", log)

    def test_lock_analysis_failure_does_not_regenerate_or_stage(self):
        result, log = self.run_script(
            "bazel/tools/format/update-python-requirements-if-needed.sh", LOCK_STATUS="1"
        )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("bazel run", log)
        self.assertNotIn("git add", log)

    def test_ci_serial_update_and_unconditional_check(self):
        workflow = (REPO / "buildbuddy.yaml").read_text()
        locks = workflow.index("./bazel/tools/format/update-python-requirements-if-needed.sh")
        update = workflow.index("./bazel/tools/ci/python-manifest.sh update")
        formatter = workflow.index("bazel run //bazel/tools/format:format")
        writeback = workflow.index("                git add -u\n", formatter)
        check = workflow.index('./bazel/tools/ci/python-manifest.sh check "$base_ref"')
        self.assertIn('git diff --quiet "$base_ref" -- pyproject.toml bazel/requirements', workflow)
        self.assertLess(locks, update)
        self.assertLess(update, formatter)
        self.assertLess(formatter, writeback)
        self.assertLess(writeback, check)
        self.assertIn('\n          ./bazel/tools/ci/python-manifest.sh check "$base_ref"\n', workflow)
        self.assertIn('if [ "$AUTHOR" = "ci-format-bot" ]; then', workflow[:update])
        self.assertIn('elif [ "$ON_MAIN" = true ]; then', workflow[formatter:writeback])
        self.assertIn("gh-readonly-queue/*)", workflow[formatter:writeback])


class LinuxWheelsTest(unittest.TestCase):
    def test_filter_matches_universal_lock_markers(self):
        environment = {
            **default_environment(),
            "sys_platform": "linux",
            "os_name": "posix",
            "platform_system": "Linux",
            "platform_machine": "x86_64",
            "python_version": "3.13",
            "python_full_version": "3.13.9",
            "implementation_name": "cpython",
            "implementation_version": "3.13.9",
            "platform_python_implementation": "CPython",
        }
        wheels = {}
        expected = []
        for line in (REPO / "bazel/requirements/all.txt").read_text().splitlines():
            if not re.match(r"^[\w.-]+==", line):
                continue
            requirement = Requirement(line.removesuffix("\\").strip())
            name = re.sub(r"[-_.]+", "_", requirement.name).lower()
            wheel = f"@pip//{name}:whl"
            wheels[name] = wheel
            if requirement.marker is None or requirement.marker.evaluate(environment):
                expected.append(wheel)
        linux_wheels = runpy.run_path(REPO / "bazel/tools/ci/python_manifest_wheels.bzl")["linux_wheels"]
        self.assertTrue(wheels)
        self.assertNotIn("@pip//httpx2_jsfetch:whl", expected)
        self.assertIn("@pip//httpcore2:whl", expected)
        self.assertIn("@pip//jeepney:whl", expected)
        self.assertEqual(linux_wheels(wheels), expected)
        self.assertEqual(linux_wheels({"future_package": "@pip//future_package:whl"}), ["@pip//future_package:whl"])


if __name__ == "__main__":
    unittest.main()
