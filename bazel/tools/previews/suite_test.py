"""Fail-closed fixture selection, screenshot matrices and public assembly."""

import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import publish
import suite

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
SHA = "a" * 40


class SuiteTests(unittest.TestCase):
    def test_owner_capture_contracts(self):
        self.assertEqual(len(suite.expected_captures("agents")), 48)
        self.assertIn(
            "agents-390-dark-no-js-full.png", suite.expected_captures("agents")
        )
        self.assertIn(
            "agents-1440-light-normal-resume.png", suite.expected_captures("agents")
        )
        self.assertEqual(len(suite.expected_captures("trips")), 32)
        self.assertEqual(len(suite.expected_captures("stars")), 8)
        self.assertEqual(len(suite.expected_captures("review")), 16)
        self.assertIn("timeline-390-dark-viewer.png", suite.expected_captures("trips"))
        self.assertIn("timeline-1440-light-map.png", suite.expected_captures("trips"))
        self.assertIn("day-390-dark-fullscreen.png", suite.expected_captures("trips"))
        self.assertIn(
            "day-1440-light-map-controls.png", suite.expected_captures("trips")
        )
        self.assertEqual(len(suite.expected_captures("blog")), 9)
        self.assertEqual(len(suite.expected_captures("factory")), 40)

    def test_missing_day_fullscreen_capture_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in suite.expected_captures("trips"):
                (root / name).write_bytes(PNG)
            (root / "day-390-dark-fullscreen.png").unlink()
            with self.assertRaisesRegex(
                ValueError, "missing capture day-390-dark-fullscreen"
            ):
                suite.validate_captures("trips", root)

    def test_missing_agents_static_capture_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in suite.expected_captures("agents"):
                (root / name).write_bytes(PNG)
            (root / "agents-390-dark-no-js-full.png").unlink()
            with self.assertRaisesRegex(
                ValueError, "missing capture agents-390-dark-no-js-full"
            ):
                suite.validate_captures("agents", root)

    def test_missing_empty_and_invalid_capture_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "missing capture"):
                suite.validate_captures("stars", root)
            for name in suite.expected_captures("stars"):
                (root / name).write_bytes(PNG)
            self.assertEqual(len(suite.validate_captures("stars", root)), 8)
            path = root / sorted(suite.expected_captures("stars"))[0]
            for data in (b"", b"this is not a PNG" * 4):
                path.write_bytes(data)
                with self.assertRaisesRegex(ValueError, "invalid PNG"):
                    suite.validate_captures("stars", root)

    def test_discovery_requires_registered_existing_harness(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(suite, "FRONTEND", Path(directory)):
                with self.assertRaisesRegex(ValueError, "missing blog"):
                    suite.selected_suites()
                with self.assertRaisesRegex(ValueError, "unregistered"):
                    suite.selected_suites(["arbitrary-command"])
                with self.assertRaisesRegex(ValueError, "missing trips"):
                    suite.selected_suites(["trips"])
                path = Path(directory) / "trips-preview"
                path.mkdir()
                (path / "vite.config.js").touch()
                self.assertEqual(suite.selected_suites(["trips"]), ["trips"])
                for name in suite.REQUIRED_SUITES:
                    path = Path(directory) / suite.SUITES[name][0]
                    path.mkdir()
                    (path / "vite.config.js").touch()
                self.assertEqual(
                    suite.selected_suites(), ["blog", "factory", "review", "trips"]
                )
                path = Path(directory) / "agents-preview"
                path.mkdir()
                (path / "vite.config.js").touch()
                self.assertEqual(
                    suite.selected_suites(),
                    ["blog", "factory", "review", "trips", "agents"],
                )
                with self.assertRaisesRegex(ValueError, "duplicate"):
                    suite.selected_suites(["trips", "trips"])

    def test_gallery_copies_only_required_images_and_keeps_csp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence" / "stars"
            evidence.mkdir(parents=True)
            source = root / "unpublishable-map"
            source.mkdir()
            (source / "worker.js").write_text("NEVER PUBLISH THIS")
            (evidence / "failure-trace.zip").write_bytes(b"private-to-evidence")
            for name in suite.expected_captures("stars"):
                (evidence / name).write_bytes(PNG)
            output = root / "site"
            suite.compose(output, {"stars": source}, evidence.parent, SHA)
            files = suite.digest_tree(output)
            self.assertEqual(len([name for name in files if name.endswith(".png")]), 8)
            self.assertFalse(any(name.endswith((".js", ".zip")) for name in files))
            for path in output.rglob("*.html"):
                publish.validate_html(path.read_bytes())
            self.assertEqual(
                json.loads((output / "build.json").read_text())["commit"], SHA
            )

    def test_interactive_bytes_are_unchanged_and_unsafe_html_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            original = suite.page("Synthetic controls", "<p>Invented</p>")
            (source / "index.html").write_bytes(original)
            suite.compose(root / "good", {"review": source}, root, SHA)
            self.assertEqual(
                (root / "good/suites/review/index.html").read_bytes(), original
            )
            (source / "index.html").write_text("<html><body>No CSP</body></html>")
            with self.assertRaises(publish.Unsafe):
                suite.compose(root / "bad", {"review": source}, root, SHA)

    def test_hashes_reject_symlinks_and_empty_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "empty artifact"):
                suite.digest_tree(root)
            (root / "link").symlink_to(root / "missing")
            with self.assertRaisesRegex(ValueError, "symlink"):
                suite.digest_tree(root)


if __name__ == "__main__":
    unittest.main()
