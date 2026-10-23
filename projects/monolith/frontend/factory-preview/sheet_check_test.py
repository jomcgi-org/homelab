"""The repeatability check requires exact pixels, not a visual tolerance."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call

from PIL import Image

from sheet_check import compare, viewport_page_capture


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

    def test_overflow_keeps_raw_image_and_equal_viewport_capture_surface(self):
        page = Mock()
        page.evaluate.side_effect = [1255, 396]
        path = self.output / "blog.png"
        viewport_page_capture(page, path, 390)
        self.assertEqual(
            page.screenshot.call_args_list,
            [
                call(
                    path=str(self.output / "blog-overflow.png"),
                    full_page=True,
                    animations="disabled",
                ),
                call(
                    path=str(path),
                    clip={"x": 0, "y": 0, "width": 390, "height": 1255},
                    animations="disabled",
                ),
            ],
        )

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


if __name__ == "__main__":
    unittest.main()
