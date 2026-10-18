"""Outer orchestration regressions; actual stale-input tests live with the macro."""

import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import tempfile
import textwrap
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
  'run //bazel/requirements:requirements.all')
    if [[ "${LOCK_UPDATE_STATUS:-0}" != 0 ]]; then exit "$LOCK_UPDATE_STATUS"; fi
    touch "$LOCK_REPAIRED" ;;
  'test //bazel/tools/python:gazelle_python_manifest.test --config=ci')
    if [[ ! -e "$REPAIRED" ]]; then exit "${INTEGRITY_STATUS:-0}"; fi ;;
  'run //bazel/tools/python:gazelle_python_manifest.update'*)
    if [[ "${UPDATE_STATUS:-0}" != 0 ]]; then exit "$UPDATE_STATUS"; fi
    touch "$REPAIRED" ;;
  'test //bazel/tools/python:gazelle_python_manifest.test //bazel/tools/python:manifest_generated_test'*) exit "${RECHECK_STATUS:-0}" ;;
  'test //bazel/tools/python:gazelle_python_manifest.test //bazel/tools/python:manifest_drift_test'*) exit "${CHECK_STATUS:-0}" ;;
  'test //bazel/tools/python:manifest_generated_test'*) exit "${GENERATED_STATUS:-0}" ;;
  'test '*)
    if [[ "${REQUIRE_CURRENT_LOCKS:-0}" == 1 && ! -e "$LOCK_REPAIRED" ]]; then
      echo 'runtime.txt is out-of-date' >&2
      exit 3
    fi ;;
esac
""",
            "git": """
printf 'git %s\\n' "$*" >>"$COMMAND_LOG"
if [[ "$1" == diff ]]; then exit "${INPUTS_CHANGED:-0}"; fi
if [[ "$1" == merge-base ]]; then echo branch-base; fi
if [[ "$1" == log ]]; then echo "${FAKE_AUTHOR:-developer}"; fi
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
            "LOCK_REPAIRED": str(self.root / "locks-repaired"),
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

    def test_regeneration_analysis_failure_stops_before_recheck(self):
        result, log = self.driver("update", INTEGRITY_STATUS="3", UPDATE_STATUS="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "bazel run //bazel/tools/python:gazelle_python_manifest.update", log
        )
        self.assertNotIn("manifest_generated_test", log)
        self.assertFalse((self.root / "repaired").exists())

    def test_regeneration_requires_passing_integrity_and_generated_bytes(self):
        result, log = self.driver("update", INTEGRITY_STATUS="3", RECHECK_STATUS="3")
        self.assertEqual(result.returncode, 3)
        self.assertIn("manifest_generated_test --config=ci --nocache_test_results", log)
        self.assertEqual(log.count("bazel run"), 1)

    def test_generated_byte_drift_fails_read_only_check(self):
        result, log = self.driver("check", INPUTS_CHANGED="1", GENERATED_STATUS="3")
        self.assertEqual(result.returncode, 3)
        self.assertIn("manifest_generated_test --config=ci --nocache_test_results", log)
        self.assertNotIn("bazel run", log)

    def test_unsupported_or_nested_execution_is_refused(self):
        for env in ({"FAKE_OS": "Darwin"}, {"BUILD_WORKSPACE_DIRECTORY": "/nested"}):
            result, log = self.driver("update", **env)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(log, "")

    def test_full_update_keeps_renovate_lock_only(self):
        result, log = self.run_script(
            "bazel/tools/format/update-python-requirements.sh"
        )
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
        self.assertNotIn("git add", log)
        self.assertNotIn("updated and staged", result.stdout)
        self.assertNotIn("gazelle_python.yaml", log)
        self.assertNotIn("bazel run //bazel/requirements", log)

    def test_aggregate_drift_delegates_full_update(self):
        result, log = self.run_script(
            "bazel/tools/format/update-python-requirements-if-needed.sh",
            LOCK_STATUS="3",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bazel run //bazel/requirements:requirements.all", log)
        self.assertNotIn("gazelle_python_manifest", log)
        self.assertIn("git add -u --", log)
        self.assertIn("updated and staged", result.stdout)

    def test_darwin_hook_defers_without_bazel_or_staging(self):
        result, log = self.run_script(
            "bazel/tools/format/update-python-requirements-if-needed.sh",
            FAKE_OS="Darwin",
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
            "bazel/tools/format/update-python-requirements-if-needed.sh",
            LOCK_STATUS="1",
        )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("bazel run", log)
        self.assertNotIn("git add", log)

    def test_ci_serial_update_and_unconditional_check(self):
        workflow = (REPO / "buildbuddy.yaml").read_text()
        locks = workflow.index(
            "./bazel/tools/format/update-python-requirements-if-needed.sh"
        )
        update = workflow.index("./bazel/tools/ci/python-manifest.sh update")
        formatter = workflow.index("bazel run //bazel/tools/format:format")
        writeback = workflow.index("                git add -u\n", formatter)
        check = workflow.index('./bazel/tools/ci/python-manifest.sh check "$base_ref"')
        self.assertIn(
            'git diff --quiet "$base_ref" -- pyproject.toml bazel/requirements',
            workflow,
        )
        self.assertLess(locks, update)
        self.assertLess(locks, workflow.index("# ---- 2. tests"))
        self.assertLess(update, formatter)
        self.assertLess(formatter, writeback)
        self.assertLess(writeback, check)
        self.assertIn(
            '\n          ./bazel/tools/ci/python-manifest.sh check "$base_ref"\n',
            workflow,
        )
        self.assertIn('if [ "$AUTHOR" = "ci-format-bot" ]; then', workflow[:update])
        self.assertIn('elif [ "$ON_MAIN" = true ]; then', workflow[formatter:writeback])
        self.assertIn("gh-readonly-queue/*)", workflow[formatter:writeback])

    def run_ci_lock_tests(self, **env):
        workflow = (REPO / "buildbuddy.yaml").read_text()
        start = workflow.index(
            "\n", workflow.index("python3 ./bazel/tools/ci/test_source_reachability.py")
        )
        end = workflow.index("          # ---- 3. deploy", start)
        selection = self.root / "bazel/tools/ci/affected-targets.sh"
        selection.write_text("#!/bin/sh\necho //bazel/requirements:runtime_test\n")
        selection.chmod(0o755)
        script = self.root / "lock-tests.sh"
        script.write_text("set -euo pipefail\n" + textwrap.dedent(workflow[start:end]))
        return self.run_script(
            "lock-tests.sh",
            **{
                "ON_MAIN": "false",
                "GIT_BRANCH": "factory/test",
                "GIT_PR_NUMBER": "7169",
                "base_ref": "branch-base",
                "INPUTS_CHANGED": "1",
                "LOCK_STATUS": "3",
                "REQUIRE_CURRENT_LOCKS": "1",
                **env,
            },
        )

    def test_ci_repairs_stale_locks_before_ordinary_tests(self):
        result, log = self.run_ci_lock_tests()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(
            log.index("bazel run //bazel/requirements:requirements.all"),
            log.index("bazel test --config=ci"),
        )

    def test_ci_lock_repair_failure_stops_before_ordinary_tests(self):
        result, log = self.run_ci_lock_tests(LOCK_UPDATE_STATUS="1")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("bazel test --config=ci", log)
        self.assertNotIn("git add", log)

    def test_ci_read_only_heads_reject_stale_locks_without_repair(self):
        for env in (
            {"ON_MAIN": "true", "GIT_BRANCH": "main", "GIT_PR_NUMBER": "0"},
            {"GIT_BRANCH": "gh-readonly-queue/main/pr-7169"},
            {"FAKE_AUTHOR": "ci-format-bot"},
        ):
            with self.subTest(env=env):
                result, log = self.run_ci_lock_tests(**env)
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn("runtime.txt is out-of-date", result.stderr)
                self.assertNotIn("bazel run", log)
                self.assertNotIn("git add", log)

    def test_ci_skips_lock_repair_when_inputs_are_unchanged(self):
        result, log = self.run_ci_lock_tests(
            INPUTS_CHANGED="0", REQUIRE_CURRENT_LOCKS="0"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("bazel test //bazel/requirements:", log)
        self.assertNotIn("bazel run", log)

    def test_ci_drift_check_detects_staged_only_runtime_repair(self):
        workflow = (REPO / "buildbuddy.yaml").read_text()
        command = re.search(r"if (git diff --exit-code[^;]*); then", workflow).group(1)
        repo = self.root / "writeback"
        repo.mkdir()

        def git(*args):
            return subprocess.run(
                ["git", *args],
                cwd=repo,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "Test",
                    "GIT_AUTHOR_EMAIL": "test@example.com",
                    "GIT_COMMITTER_NAME": "Test",
                    "GIT_COMMITTER_EMAIL": "test@example.com",
                },
                check=True,
                capture_output=True,
                text=True,
            )

        git("init")
        lock = repo / "runtime.txt"
        lock.write_text("old lock\n")
        git("add", "runtime.txt")
        git("commit", "-m", "fixture")
        for staged in (False, True):
            with self.subTest(staged=staged):
                lock.write_text("repaired lock\n")
                if staged:
                    git("add", "runtime.txt")
                result = subprocess.run(
                    ["bash", "-c", command], cwd=repo, capture_output=True, text=True
                )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        git("commit", "-m", "repair")
        result = subprocess.run(
            ["bash", "-c", command], cwd=repo, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)


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
        linux_wheels = runpy.run_path(
            REPO / "bazel/tools/ci/python_manifest_wheels.bzl"
        )["linux_wheels"]
        self.assertTrue(wheels)
        self.assertNotIn("@pip//httpx2_jsfetch:whl", expected)
        self.assertIn("@pip//httpcore2:whl", expected)
        self.assertIn("@pip//jeepney:whl", expected)
        self.assertEqual(linux_wheels(wheels), expected)
        self.assertEqual(
            linux_wheels({"future_package": "@pip//future_package:whl"}),
            ["@pip//future_package:whl"],
        )


if __name__ == "__main__":
    unittest.main()
