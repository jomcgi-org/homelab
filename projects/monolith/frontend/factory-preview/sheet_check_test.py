"""The repeatability check requires exact pixels, not a visual tolerance."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from sheet_check import compare, validate_capture, viewport_page_capture


class ComparisonTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.before = self.root / "before"
        self.after = self.root / "after"
        self.output = self.root / "comparison"
        for path in [self.before, self.after, self.output]:
            path.mkdir()
        for path in [self.before, self.after]:
            Image.new("RGB", (20, 20), "white").save(path / "journal.png")
            self.write_results(path)

    def write_results(self, path, *, probes=None, overflow="auto"):
        geometry = {
            "elements": [
                {
                    "selector": "body",
                    "style": {
                        "overflowX": overflow,
                        "overflowY": overflow,
                        "position": "static",
                        "top": "auto",
                    },
                }
            ]
        }
        (path / "results.json").write_text(
            json.dumps(
                {
                    "revision": path.name,
                    "build": {"harness_sha": "same-harness"},
                    "captures": [
                        {
                            "key": "journal",
                            "opening": {"geometry": geometry},
                            "scrolled": geometry,
                            "repeatability_images": probes or ["journal.png"],
                        }
                    ],
                }
            )
        )

    def compare(self):
        with redirect_stdout(io.StringIO()):
            return compare(self.before, self.after, self.output, acceptance=True)

    def test_identical_pixels_pass(self):
        self.assertEqual(self.compare()["regressions"], [])

    def test_single_changed_pixel_fails(self):
        image = Image.open(self.after / "journal.png")
        image.putpixel((10, 10), (0, 0, 0))
        image.save(self.after / "journal.png")
        with self.assertRaisesRegex(AssertionError, "visible pixel regression"):
            self.compare()

    def test_probe_must_match_entire_actual_capture(self):
        Image.new("RGB", (20, 20), "black").save(self.after / "journal.png")
        Image.new("RGB", (20, 20), "black").save(self.before / "repeat.png")
        self.write_results(self.before, probes=["journal.png", "repeat.png"])
        result = self.compare()
        self.assertEqual(
            result["differences"][0]["exact_repeatability_match"],
            {"base": "repeat.png", "head": "journal.png"},
        )
        image = Image.open(self.after / "journal.png")
        image.putpixel((10, 10), (255, 0, 0))
        image.save(self.after / "journal.png")
        with self.assertRaisesRegex(AssertionError, "visible pixel regression"):
            self.compare()

    def test_identical_pixels_do_not_excuse_scroll_owner_change(self):
        self.write_results(self.after, overflow="hidden")
        with self.assertRaisesRegex(AssertionError, "overflowY"):
            self.compare()

    def test_document_height_change_fails(self):
        Image.new("RGB", (20, 21), "white").save(self.after / "journal.png")
        with self.assertRaisesRegex(AssertionError, "document height changed"):
            self.compare()

    def test_identical_pixels_do_not_excuse_keyboard_focus_change(self):
        path = self.after / "results.json"
        result = json.loads(path.read_text())
        result["captures"][0]["checks"] = {"focus": [{"radius": "0px"}]}
        path.write_text(json.dumps(result))
        with self.assertRaisesRegex(AssertionError, "keyboard focus treatment changed"):
            self.compare()


@unittest.skipUnless(
    os.environ.get("SHEET_REAL_BROWSER") == "1", "real Chromium runs in preview CI"
)
class BrowserCaptureTest(unittest.TestCase):
    def test_real_long_page_and_corrupt_evidence(self):
        from playwright.sync_api import sync_playwright

        with tempfile.TemporaryDirectory() as directory, sync_playwright() as pw:
            browser = pw.chromium.launch()
            for width in (390, 1440):
                page = browser.new_page(viewport={"width": width, "height": 900})
                page.set_content(
                    "<style>body{margin:0;height:6352px}</style><button>Capture</button>"
                )
                page.get_by_role("button", name="Capture").click()
                path = Path(directory) / f"{width}.png"
                manifest = viewport_page_capture(page, path, width)
                self.assertEqual(manifest["document"]["height"], 6352)
                self.assertEqual(len(manifest["segments"]), 8)
                validate_capture(path)
                Image.new("RGB", (width, 900)).save(path)
                with self.assertRaisesRegex(AssertionError, "stale evidence"):
                    validate_capture(path)
                path.unlink()
                with self.assertRaises(FileNotFoundError):
                    validate_capture(path)
                page.close()
            browser.close()


if __name__ == "__main__":
    unittest.main()
