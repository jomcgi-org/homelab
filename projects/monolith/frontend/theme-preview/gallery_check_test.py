"""Fail-closed gallery preflight and evidence contract, without a browser."""

import hashlib
import itertools
import tempfile
import unittest
from pathlib import Path

import gallery_check as gallery


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.evidence = {"gallery_cases": [], "screenshots": {}}
        for composition, width, scheme, scale in itertools.product(
            gallery.COMPOSITIONS, gallery.WIDTHS, gallery.SCHEMES, gallery.SCALES
        ):
            name = f"{composition}-{width}-{scheme}-{scale * 100}pct"
            screenshots = [
                f"gallery/{name}-{boundary}-{suffix}.png"
                for boundary in gallery.SCHEMES
                for suffix in gallery.CAPTURES
            ]
            screenshots.append(f"gallery/{name}-full.png")
            for screenshot in screenshots:
                path = self.root / screenshot
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(b"synthetic screenshot evidence")
                self.evidence["screenshots"][screenshot] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
            self.evidence["gallery_cases"].append(
                {
                    "name": name,
                    "status": "passed",
                    "screenshots": screenshots,
                    "repeat_digest_identical": True,
                }
            )

    def test_missing_page(self):
        with self.assertRaisesRegex(AssertionError, "gallery page missing"):
            gallery.preflight(self.root)

    def test_empty_page(self):
        (self.root / "gallery.html").touch()
        with self.assertRaisesRegex(AssertionError, "gallery page missing"):
            gallery.preflight(self.root)

    def test_zero_cases(self):
        with self.assertRaisesRegex(AssertionError, "zero gallery cases ran"):
            gallery.validate({"gallery_cases": [], "screenshots": {}}, self.root)

    def test_incomplete_matrix(self):
        self.evidence["gallery_cases"].pop()
        with self.assertRaisesRegex(AssertionError, "matrix incomplete"):
            gallery.validate(self.evidence, self.root)

    def test_missing_capture_declaration(self):
        self.evidence["gallery_cases"][0]["screenshots"].pop()
        with self.assertRaisesRegex(AssertionError, "screenshot list incomplete"):
            gallery.validate(self.evidence, self.root)

    def test_missing_and_empty_screenshot(self):
        path = self.root / self.evidence["gallery_cases"][0]["screenshots"][0]
        path.unlink()
        with self.assertRaisesRegex(AssertionError, "missing or empty screenshot"):
            gallery.validate(self.evidence, self.root)
        path.touch()
        with self.assertRaisesRegex(AssertionError, "missing or empty screenshot"):
            gallery.validate(self.evidence, self.root)

    def test_digest_mismatch(self):
        (self.root / self.evidence["gallery_cases"][0]["screenshots"][0]).write_bytes(
            b"changed"
        )
        with self.assertRaisesRegex(AssertionError, "digest mismatch"):
            gallery.validate(self.evidence, self.root)

    def test_failed_case_requires_trace(self):
        case = self.evidence["gallery_cases"][0]
        case.update(status="failed", trace="absent.zip")
        with self.assertRaisesRegex(AssertionError, "no trace"):
            gallery.validate(self.evidence, self.root)
        (self.root / "absent.zip").write_bytes(b"trace")
        with self.assertRaisesRegex(AssertionError, "gallery checks failed"):
            gallery.validate(self.evidence, self.root)

    def test_repeat_required(self):
        for case in self.evidence["gallery_cases"]:
            case["repeat_digest_identical"] = False
        with self.assertRaisesRegex(AssertionError, "determinism evidence missing"):
            gallery.validate(self.evidence, self.root)

    def test_complete_matrix(self):
        gallery.validate(self.evidence, self.root)


if __name__ == "__main__":
    unittest.main()
