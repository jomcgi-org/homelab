"""Preparation/rollback tests plus optional real corrected-harness validation."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import browser_handoff_prepare as prepare


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.preview = self.root / "projects/monolith/frontend/factory-preview"
        self.preview.mkdir(parents=True)
        self.fake_helper = self.preview / "browser_handoff_prepare.py"
        (self.preview / "sheet_check.py").write_text("# existence fixture only\n")
        # A subprocess fixture tests transport to the checker, not bundle
        # acceptance. The separate real-harness test below exercises rejection.
        (self.preview / "browser_handoff_check.py").write_text(
            "import hashlib,json,sys\n"
            "from pathlib import Path\n"
            "r=json.load(sys.stdin)\n"
            "assert r['handoff']['producer']['run_id']=='unbound-local-validation'\n"
            "assert sys.flags.isolated==1\n"
            "root=Path(r['checkout'])/r['handoff']['evidence_root']\n"
            "if (root/'reject').exists(): raise ValueError('clipped evidence fixture')\n"
            "h=r['handoff']; p=h['provenance']\n"
            "entries=json.loads((root/'manifest.json').read_text())['files']\n"
            "v={k:h[k] for k in ('manifest_sha256','inventory_sha256')}\n"
            "v.update({k:p[k] for k in ('base_sha','head_sha','harness_sha')})\n"
            "v.update(status='ok',files=len(entries),bytes=sum(e['size'] for e in entries),"
            "handoff_sha256=hashlib.sha256(json.dumps(h,sort_keys=True,separators=(',',':')).encode()).hexdigest())\n"
            "if (root/'wrong-verdict').exists(): v['head_sha']='f'*40\n"
            "print(json.dumps(v))\n"
        )
        self.evidence = self.root / "output/sheets"
        for side, revision in (("before", "a" * 40), ("after", "b" * 40)):
            directory = self.evidence / side
            directory.mkdir(parents=True)
            (directory / "results.json").write_text(
                json.dumps(
                    {
                        "revision": revision,
                        "build": {"harness_sha": "d" * 40, "production_sha": revision},
                        "chromium_version": "131.0",
                        "captures": [],
                    }
                )
            )
        self.args = argparse.Namespace(
            checkout=str(self.root),
            evidence_root="output/sheets",
            repo="jomcgi-org/homelab",
            base_sha="a" * 40,
            head_sha="b" * 40,
            target_sha="c" * 40,
            harness_sha="d" * 40,
            browser_version="131.0",
            output="handoff.json",
        )
        self.source_provenance = {
            "base_sha": self.args.base_sha,
            "target_sha": self.args.target_sha,
            "head_sha": self.args.head_sha,
            "harness_sha": self.args.harness_sha,
            "base_updates_source_sha256": "e" * 64,
            "base_updates_theme_contract": "historical-updates-os",
            "role": "capture-only",
            "visual_acceptance": "independent review required",
        }
        self.provenance_bytes = (
            json.dumps(self.source_provenance, indent=2) + "\n"
        ).encode()
        (self.evidence / "provenance.json").write_bytes(self.provenance_bytes)
        for kind in prepare.INPUT_KINDS:
            source = self.root / (kind + ".source")
            source.write_bytes((kind + " actual bytes\x00").encode())
            setattr(self.args, kind, str(source))

    def run_prepare(self):
        with patch.object(prepare, "__file__", str(self.fake_helper)):
            return prepare.prepare(self.args)

    def test_capture_declaration_omits_server_identity_and_binds_exact_inputs(self):
        outcome = self.run_prepare()
        self.assertEqual(outcome["status"], "capture-complete")
        declaration = json.loads((self.root / "handoff.json").read_text())
        self.assertNotIn("producer", declaration)
        self.assertNotIn("unbound-local-validation", json.dumps(declaration))
        self.assertEqual(declaration["role"], "capture-only")
        manifest_raw = (self.evidence / "manifest.json").read_bytes()
        self.assertEqual(
            declaration["manifest_sha256"], hashlib.sha256(manifest_raw).hexdigest()
        )
        files = json.loads(manifest_raw)["files"]
        self.assertEqual(
            declaration["inventory_sha256"],
            hashlib.sha256(prepare.encoded(files)).hexdigest(),
        )
        entries = {entry["path"]: entry for entry in files}
        for kind in prepare.INPUT_KINDS:
            content = Path(getattr(self.args, kind)).read_bytes()
            self.assertEqual((self.evidence / "inputs" / kind).read_bytes(), content)
            self.assertEqual(
                declaration["provenance"][kind + "_sha256"],
                hashlib.sha256(content).hexdigest(),
            )
            self.assertEqual(entries["inputs/" + kind]["size"], len(content))
        self.assertEqual(
            (self.evidence / "provenance.json").read_bytes(), self.provenance_bytes
        )
        self.assertEqual(
            entries["provenance.json"]["sha256"],
            hashlib.sha256(self.provenance_bytes).hexdigest(),
        )
        self.assertNotIn("manifest.json", entries)
        self.assertNotIn("handoff.json", entries)

    def test_checker_failure_rolls_back_metadata_but_preserves_capture(self):
        (self.evidence / "reject").write_text("failure fixture")
        with self.assertRaisesRegex(ValueError, "clipped evidence fixture"):
            self.run_prepare()
        self.assertTrue((self.evidence / "before/results.json").exists())
        self.assertFalse((self.root / "handoff.json").exists())
        for name in ("inputs", "inputs.json", "manifest.json"):
            self.assertFalse((self.evidence / name).exists())

    def test_source_provenance_mismatch_fails_before_writes(self):
        for name in ("base_sha", "target_sha", "head_sha", "harness_sha"):
            with self.subTest(field=name):
                original = getattr(self.args, name)
                setattr(self.args, name, "f" * 40)
                with self.assertRaisesRegex(
                    ValueError, "stale comparison provenance: " + name
                ):
                    self.run_prepare()
                setattr(self.args, name, original)
                self.assertEqual(
                    (self.evidence / "provenance.json").read_bytes(),
                    self.provenance_bytes,
                )
                for created in ("inputs", "inputs.json", "manifest.json"):
                    self.assertFalse((self.evidence / created).exists())
                self.assertFalse((self.root / "handoff.json").exists())

    def test_missing_source_provenance_fails_before_writes(self):
        (self.evidence / "provenance.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.run_prepare()
        self.assertFalse((self.evidence / "inputs").exists())

    def test_checker_verdict_mismatch_rejected(self):
        (self.evidence / "wrong-verdict").write_text("failure fixture")
        with self.assertRaisesRegex(ValueError, "mismatched verification"):
            self.run_prepare()
        self.assertFalse((self.root / "handoff.json").exists())
        self.assertEqual(
            (self.evidence / "provenance.json").read_bytes(), self.provenance_bytes
        )

    def test_stale_output_never_overwritten(self):
        (self.root / "handoff.json").write_text("previous")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.run_prepare()
        self.assertEqual((self.root / "handoff.json").read_text(), "previous")

    def test_unsafe_paths_and_symlinks_rejected(self):
        for path in ("../escape", "/absolute", "output/../sheets", "output//sheets"):
            self.args.evidence_root = path
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.run_prepare()
        self.args.evidence_root = "output/sheets"
        (self.evidence / "link").symlink_to(self.root / "fixture.source")
        with self.assertRaisesRegex(ValueError, "unsafe evidence"):
            self.run_prepare()

    def test_missing_corrected_harness_is_explicit(self):
        (self.preview / "sheet_check.py").unlink()
        with self.assertRaisesRegex(ValueError, "missing corrected sheet_check"):
            self.run_prepare()

    @unittest.skipUnless(
        os.environ.get("SHEET_HANDOFF_HARNESS"),
        "requires reviewed corrected sheet harness",
    )
    def test_real_checker_rejects_missing_matrix_and_cleans_metadata(self):
        here = Path(prepare.__file__).resolve()
        shutil.copyfile(
            here.with_name("browser_handoff_check.py"),
            self.preview / "browser_handoff_check.py",
        )
        shutil.copyfile(
            os.environ["SHEET_HANDOFF_HARNESS"], self.preview / "sheet_check.py"
        )
        contract = here.parents[2] / "factory/orchestration/browser_evidence.py"
        target = (
            self.root / "projects/monolith/factory/orchestration/browser_evidence.py"
        )
        target.parent.mkdir(parents=True)
        shutil.copyfile(contract, target)
        with self.assertRaisesRegex(ValueError, "missing or duplicate checklist cases"):
            self.run_prepare()
        self.assertFalse((self.root / "handoff.json").exists())
        self.assertFalse((self.evidence / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
