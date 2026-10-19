"""Require real revision labels and matching capture inputs before pairing."""

import json
from pathlib import Path
import tempfile
import unittest
import struct
import zlib

import compare
import suite

HEAD = "a" * 40
BASE = "b" * 40


def png(width, height=1000):
    def chunk(kind, data):
        return (
            struct.pack("!I", len(data))
            + kind
            + data
            + struct.pack("!I", zlib.crc32(kind + data))
        )

    pixels = (b"\0" + b"\xff" * (width * 3)) * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


class CompareTests(unittest.TestCase):
    def setup_artifacts(self, root, name, baseline=True):
        for side, sha in (("head", HEAD), ("before", BASE)):
            if side == "before" and not baseline:
                continue
            artifact = root / (side + "-artifact")
            artifact.mkdir()
            (artifact / "index.html").write_bytes(
                suite.page("Synthetic demo", "Invented data")
            )
            evidence = root / "evidence" / side / name
            evidence.mkdir(parents=True)
            for filename in suite.expected_captures(name):
                (evidence / filename).write_bytes(
                    png(compare.case_details(name, filename)[1])
                )
            record = {
                "suite": name,
                "playwright": "1.63.0",
                "chromium": "153.0.8010.12",
                "screenshots": suite.validate_captures(name, evidence),
            }
            (evidence.parent / "suite.json").write_text(
                json.dumps({"commit": sha, "status": "passed", "suites": [record]})
            )

    def assemble(self, root, name="blog", baseline=True, fingerprints=None):
        return compare.assemble(
            root / "site",
            root / "head-artifact",
            root / "before-artifact",
            root / "evidence",
            HEAD,
            BASE,
            [name],
            [name] if baseline else [],
            fingerprints or {name: {"head": "same", "before": "same"}},
        )

    def test_same_inputs_produce_labeled_pairs_at_real_revisions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.setup_artifacts(root, "blog")
            model = self.assemble(root)
            self.assertEqual(model["baseline"], BASE)
            self.assertEqual(model["head"], HEAD)
            self.assertEqual(len(model["rows"]), 9)
            self.assertTrue(
                all(row["before"] and row["identical"] for row in model["rows"])
            )
            self.assertIn(
                "Screenshot comparison", (root / "site/index.html").read_text()
            )

    def test_absent_owner_harness_has_no_fake_before_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.setup_artifacts(root, "trips", baseline=False)
            model = self.assemble(root, "trips", baseline=False)
            self.assertTrue(
                all(
                    row["before"] is None
                    and "no trips-preview harness" in row["unavailable"]
                    for row in model["rows"]
                )
            )
            self.assertFalse(list((root / "site/captures").rglob("before-*.png")))
            self.assertFalse((root / "site/before").exists())

    def test_changed_fixture_or_harness_is_not_comparable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.setup_artifacts(root, "blog")
            model = self.assemble(
                root,
                fingerprints={"blog": {"head": "new-inputs", "before": "old-inputs"}},
            )
            self.assertTrue(
                all(
                    row["before"] is None
                    and "inputs or capture harness differ" in row["unavailable"]
                    for row in model["rows"]
                )
            )

    def test_browser_mismatch_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.setup_artifacts(root, "blog")
            path = root / "evidence/before/suite.json"
            data = json.loads(path.read_text())
            data["suites"][0]["chromium"] = "138.0.7204.0"
            path.write_text(json.dumps(data))
            model = self.assemble(root)
            self.assertTrue(
                all(
                    row["before"] is None
                    and "Browser versions differ" in row["unavailable"]
                    for row in model["rows"]
                )
            )

    def test_missing_capture_fails_even_when_fingerprints_differ(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.setup_artifacts(root, "blog")
            (root / "evidence/before/blog/390x844-full.png").unlink()
            with self.assertRaisesRegex(ValueError, "missing capture"):
                self.assemble(
                    root, fingerprints={"blog": {"head": "new", "before": "old"}}
                )

    def test_stale_receipt_and_invalid_revision_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.setup_artifacts(root, "blog")
            path = root / "evidence/head/suite.json"
            data = json.loads(path.read_text())
            data["commit"] = "c" * 40
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "head evidence mismatch"):
                self.assemble(root)

    def test_capture_names_map_to_matched_viewport_and_scheme(self):
        self.assertEqual(
            compare.case_details(
                "factory", "overview-live-390x844-dark-100pct-full.png"
            ),
            ("overview-full", 390, "dark"),
        )
        self.assertEqual(
            compare.case_details("trips", "timeline-1440-light-viewer.png"),
            ("timeline-viewer", 1440, "light"),
        )
        self.assertEqual(
            compare.case_details("trips", "day-390-dark-fullscreen.png"),
            ("day-fullscreen", 390, "dark"),
        )
        self.assertEqual(
            compare.case_details("stars", "historical-dark-390.png"),
            ("historical", 390, "dark"),
        )
        self.assertEqual(
            compare.case_details("review", "notes-audit-390-dark.png"),
            ("notes-audit", 390, "dark"),
        )
        for name in suite.SUITES:
            for filename in suite.expected_captures(name):
                case, width, scheme = compare.case_details(name, filename)
                self.assertTrue(case)
                self.assertIn(width, (360, 390, 1440))
                self.assertIn(scheme, ("light", "dark"))

    def test_shared_fixture_wrapper_changes_invalidate_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frontend = root / "projects/monolith/frontend"
            harness = frontend / "trips-preview"
            harness.mkdir(parents=True)
            (harness / "main.js").write_text('import "../test/TripsRoute.svelte";')
            for relative in compare.EXTRA_FIXTURES["trips"]:
                path = frontend / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic test input")
            before = compare.fingerprint(root, "trips")
            with (frontend / "test/TripsRoute.svelte").open("a") as handle:
                handle.write("<h2>Changed fixture composition</h2>")
            self.assertNotEqual(before, compare.fingerprint(root, "trips"))
            before = compare.fingerprint(root, "trips")
            production = frontend / "src/routes/public/app/trips/+page.svelte"
            production.parent.mkdir(parents=True)
            production.write_text("<h1>Changed production UI</h1>")
            self.assertEqual(before, compare.fingerprint(root, "trips"))

    def test_png_width_must_match_capture_viewport(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.png"
            path.write_bytes(png(390, 4000))
            self.assertEqual(
                compare.png_dimensions(path, 390), {"width": 390, "height": 4000}
            )
            with self.assertRaisesRegex(ValueError, "dimensions"):
                compare.png_dimensions(path, 1440)


if __name__ == "__main__":
    unittest.main()
