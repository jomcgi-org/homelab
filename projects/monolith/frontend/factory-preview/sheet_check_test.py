"""The repeatability check requires exact pixels, not a visual tolerance."""

import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from contextlib import redirect_stdout
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from sheet_check import (
    CHROMIUM_ARGS,
    QuietHandler,
    capture,
    check_schemes,
    compare,
    validate_bundle,
    validate_capture,
    viewport_page_capture,
)
from sheet_ci_check import baseline_updates_os_theme


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
                    "chromium_args": CHROMIUM_ARGS,
                    "chromium_version": "test-browser",
                    "playwright_version": "test-playwright",
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
        # Comparator unit fixtures intentionally isolate pixel comparison.
        with patch("sheet_check.validate_bundle"), redirect_stdout(io.StringIO()):
            return compare(self.before, self.after, self.output, acceptance=True)

    def test_identical_pixels_pass(self):
        self.assertEqual(self.compare()["regressions"], [])

    def test_identical_pixels_require_matching_capture_environment(self):
        original = json.loads((self.after / "results.json").read_text())
        for field in ("chromium_args", "chromium_version", "playwright_version"):
            for missing in (False, True):
                with self.subTest(field=field, missing=missing):
                    changed = dict(original)
                    if missing:
                        changed.pop(field)
                    else:
                        changed[field] = (
                            ["--disable-gpu"]
                            if field == "chromium_args"
                            else "other-version"
                        )
                    (self.after / "results.json").write_text(json.dumps(changed))
                    with self.assertRaisesRegex(AssertionError, "capture environment"):
                        self.compare()

    def test_empty_image_manifest_cannot_claim_coverage(self):
        path = self.output / "missing.png"
        path.with_suffix(".capture.json").write_text(
            json.dumps(
                {
                    "document": {
                        "height": 6352,
                        "width": 390,
                        "viewportWidth": 390,
                        "viewport": 900,
                    },
                    "images": [],
                    "segments": [],
                }
            )
        )
        with self.assertRaisesRegex(AssertionError, "missing required"):
            validate_capture(path)

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


class BaselineContractTest(unittest.TestCase):
    def test_all_trace_categories_require_recorded_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            keys = [
                f"overview-{width}-{scheme}"
                for width in (390, 1440)
                for scheme in ("light", "dark")
            ]
            traces = (
                [f"{key}-trace.zip" for key in keys]
                + [
                    f"scheme-{view}-{scheme}-trace.zip"
                    for view in ("post", "overview", "updates")
                    for scheme in ("light", "dark")
                ]
                + [f"behavior-{view}-trace.zip" for view in ("activity", "overview")]
            )
            result = {
                "revision": "exact-head",
                "build": {"production_sha": "exact-head"},
                "captures": [
                    {
                        "key": key,
                        "repeatability_images": [f"{key}.png"],
                        "checks": {},
                        "opening": {"geometry": {"document": {"scrollHeight": 1}}},
                    }
                    for key in keys
                ],
                "scheme_checks": [{}] * 6,
                "behavior_checks": [{}] * 2,
            }

            def write_trace(name, member="trace.trace", data=b'{"type":"before"}\n'):
                with zipfile.ZipFile(root / name, "w") as archive:
                    if member is not None:
                        archive.writestr(member, data)

            for name in traces:
                write_trace(name)
            with (
                patch("sheet_check.VIEWS", ["overview"]),
                patch(
                    "sheet_check.validate_capture",
                    return_value={"document": {"height": 1}},
                ),
            ):
                validate_bundle(root, result)
                for name in traces:
                    for member, data in (
                        (None, b""),
                        ("trace.trace", b""),
                        ("other.json", b"{}"),
                    ):
                        with self.subTest(trace=name, member=member, data=data):
                            write_trace(name, member, data)
                            with self.assertRaisesRegex(
                                AssertionError, "missing or corrupt browser trace"
                            ):
                                validate_bundle(root, result)
                    write_trace(name)

    def test_unknown_or_mixed_source_contract_fails_closed(self):
        for source in (b"", b"window.matchMedia('dark')", b"<SchemePreference />"):
            with self.assertRaisesRegex(AssertionError, "unknown archived"):
                baseline_updates_os_theme(source)
        current = (
            b'import SchemePreference from "$lib/public/components/SchemePreference.svelte";'
            b"<SchemePreference />"
        )
        self.assertFalse(baseline_updates_os_theme(current))
        with self.assertRaisesRegex(AssertionError, "unknown archived"):
            baseline_updates_os_theme(current + b"window.matchMedia('dark')")


@unittest.skipUnless(
    os.environ.get("SHEET_REAL_BROWSER") == "1", "real Chromium runs in preview CI"
)
class BrowserCaptureTest(unittest.TestCase):
    def theme_fixture(self, browser, root, *, historical):
        (root / "sheets.html").write_text(
            """<style>main{min-height:1000px;background:rgb(255,255,255)}
            html[data-theme=dark] main, main.night{background:rgb(0,0,0)}</style>
            <main><button class="scheme">Toggle</button></main><script>
            const oldUpdates = HISTORICAL && new URL(location).searchParams.get('view') === 'updates';
            const media = matchMedia('(prefers-color-scheme: dark)');
            const button = document.querySelector('button');
            function render() {
              const scheme = oldUpdates ? (media.matches ? 'dark' : 'light') : localStorage.getItem('td-theme');
              if (oldUpdates) document.querySelector('main').className = scheme === 'dark' ? 'night' : 'day';
              else document.documentElement.dataset.theme = scheme;
              button.setAttribute('aria-label', scheme === 'dark' ? 'Switch to day scheme' : 'Switch to night scheme');
            }
            button.onclick = () => { localStorage.setItem('td-theme', localStorage.getItem('td-theme') === 'dark' ? 'light' : 'dark'); render(); };
            media.addEventListener('change', render); render();
            </script>""".replace("HISTORICAL", "true" if historical else "false")
        )
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(QuietHandler, directory=str(root))
        )
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        records = [
            {
                "key": f"{view}-390-{scheme}",
                "opening": {
                    "geometry": {
                        "elements": [
                            {"selector": "main", "style": {"backgroundColor": color}}
                        ]
                    }
                },
            }
            for view in ("post", "overview", "updates")
            for scheme, color in (
                ("light", "rgb(255, 255, 255)"),
                ("dark", "rgb(0, 0, 0)"),
            )
        ]
        return server.server_port, records

    def test_real_theme_contracts_and_wrong_expectation(self):
        from playwright.sync_api import sync_playwright

        with tempfile.TemporaryDirectory() as directory, sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                for historical in (False, True):
                    root = Path(directory) / str(historical)
                    root.mkdir()
                    port, records = self.theme_fixture(
                        browser, root, historical=historical
                    )
                    output = root / "correct"
                    output.mkdir()
                    result = check_schemes(
                        browser,
                        port,
                        records,
                        output,
                        baseline_updates_os_theme=historical,
                    )
                    self.assertEqual(len(result), 6)
                    updates = [row for row in result if row["view"] == "updates"]
                    self.assertTrue(
                        all(
                            row["opposite_os_wins"] == (not historical)
                            for row in updates
                        )
                    )
                    self.assertTrue(all(row["reload_checked"] for row in result))
                    wrong = root / "wrong"
                    wrong.mkdir()
                    with self.assertRaises(AssertionError):
                        check_schemes(
                            browser,
                            port,
                            records,
                            wrong,
                            baseline_updates_os_theme=not historical,
                        )
                    self.assertTrue((wrong / "scheme-updates-light-trace.zip").exists())
                    self.assertEqual(
                        json.loads((wrong / "failure.json").read_text())["status"],
                        "incomplete",
                    )
            finally:
                browser.close()

    def test_legacy_contract_cannot_be_used_for_head(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(AssertionError, "head acceptance"):
                capture(
                    root,
                    root / "acceptance",
                    "head",
                    acceptance=True,
                    baseline_updates_os_theme=True,
                )
            (root / "sheet-build.json").write_text(
                json.dumps(
                    {
                        "fixture_only": True,
                        "production_sha": "head",
                        "harness_sha": "head",
                    }
                )
            )
            with self.assertRaisesRegex(AssertionError, "archived baseline"):
                capture(
                    root, root / "capture-only", "head", baseline_updates_os_theme=True
                )

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
