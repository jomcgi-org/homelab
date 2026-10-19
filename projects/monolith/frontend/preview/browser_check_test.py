"""Imported suite entry point includes sheets without polluting the checkout."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import browser_check


class SheetChainTests(unittest.TestCase):
    def test_imported_check_builds_and_checks_sheets_outside_checkout(self):
        frontend = Path(browser_check.__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence"
            root = Path(directory) / "blog"
            calls = []

            def run(arguments, *, cwd, check):
                calls.append(arguments)
                self.assertEqual(cwd, frontend)
                self.assertTrue(check)
                blog.assert_called_once_with(root, output)
                if arguments[0] == "pnpm":
                    self.assertEqual(
                        arguments[:-1],
                        [
                            "pnpm",
                            "exec",
                            "vite",
                            "build",
                            "--config",
                            "sheet-preview/vite.config.js",
                            "--outDir",
                        ],
                    )
                    artifact = Path(arguments[-1])
                    self.assertFalse(artifact.is_relative_to(frontend))
                    artifact.mkdir()
                else:
                    self.assertEqual(
                        arguments,
                        [
                            sys.executable,
                            str(frontend / "sheet-preview/browser_check.py"),
                            "--artifact",
                            calls[0][-1],
                            "--output",
                            str(output / "sheets"),
                        ],
                    )
                    self.assertTrue(Path(arguments[3]).is_dir())

            with (
                patch.object(browser_check, "check_blog") as blog,
                patch.object(browser_check.subprocess, "run", side_effect=run),
            ):
                # suite.py calls this imported function; __main__ never runs.
                browser_check.check(root, output)
            self.assertEqual(len(calls), 2)
            self.assertFalse(Path(calls[0][-1]).parent.exists())

    def test_sheet_build_and_browser_failures_fail_the_suite(self):
        for successful_commands in (0, 1):
            with self.subTest(successful_commands=successful_commands):
                failure = subprocess.CalledProcessError(1, "sheet check")
                with (
                    patch.object(browser_check, "check_blog"),
                    patch.object(
                        browser_check.subprocess,
                        "run",
                        side_effect=[None] * successful_commands + [failure],
                    ) as run,
                    self.assertRaises(subprocess.CalledProcessError),
                ):
                    browser_check.check(Path("blog"), Path("evidence"))
                self.assertEqual(run.call_count, successful_commands + 1)
                artifact = Path(run.call_args_list[0].args[0][-1])
                self.assertFalse(artifact.parent.exists())

    def test_failed_blog_does_not_start_sheet_commands(self):
        with (
            patch.object(browser_check, "check_blog", side_effect=AssertionError),
            patch.object(browser_check.subprocess, "run") as run,
            self.assertRaises(AssertionError),
        ):
            browser_check.check(Path("blog"), Path("evidence"))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
