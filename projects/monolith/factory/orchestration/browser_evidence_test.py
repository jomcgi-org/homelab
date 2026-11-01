"""Contract boundary tests; no sessions, storage or network calls."""

import hashlib
import json
import tempfile
from pathlib import Path
import unittest
from dataclasses import asdict, replace

from factory.orchestration.browser_evidence import (
    MAX_HANDOFF_BYTES,
    MAX_INVENTORY_BYTES,
    EvidenceError,
    ExportProof,
    Producer,
    Provenance,
    parse_handoff,
    validate_export,
    verify_inventory,
)


class BrowserEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.provenance = Provenance(
            repo="jomcgi-org/homelab",
            base_sha="48589107e887be6f804e3b15f06771c7ad5df99a",
            head_sha="53320a841de8b60a0ff11b10804968dc56248d86",
            harness_sha="53320a841de8b60a0ff11b10804968dc56248d86",
            target_sha="53320a841de8b60a0ff11b10804968dc56248d86",
            fixture_sha256="a" * 64,
            checklist_sha256="1" * 64,
            dependency_lock_sha256="2" * 64,
            browser_name="chromium",
            browser_version="131.0.6778.33",
            browser_binary_sha256="b" * 64,
        )
        self.producer = Producer(
            run_id="browser-evidence-42",
            node_id="capture",
            attempt=1,
            session_id="producer-session",
            turn=2,
            lineage_id="workspace-lineage",
            workload="claude-runtime",
            principal="factory",
        )
        self.value = {
            "version": 1,
            "role": "capture-only",
            "status": "capture-complete",
            "provenance": asdict(self.provenance),
            "producer": asdict(self.producer),
            "evidence_root": "evidence/42",
            "manifest_path": "manifest.json",
            "manifest_sha256": "c" * 64,
            "inventory_sha256": "d" * 64,
        }
        self.handoff = self.parse()
        self.export = ExportProof(
            self.producer, self.handoff.handoff_sha256, "completed", "e" * 64
        )

    def parse(self, raw=None):
        return parse_handoff(
            json.dumps(self.value).encode() if raw is None else raw,
            expected_provenance=self.provenance,
            expected_producer=self.producer,
        )

    def test_matching_completed_export(self):
        validate_export(self.handoff, self.export)
        self.assertEqual(self.handoff.provenance, self.provenance)
        self.assertEqual(self.handoff.producer.turn, 2)

    def test_size_boundary_and_canonical_digest(self):
        raw = json.dumps(self.value).encode()
        padded = raw + b" " * (MAX_HANDOFF_BYTES - len(raw))
        self.assertEqual(self.parse(padded), self.handoff)
        with self.assertRaises(EvidenceError):
            self.parse(padded + b" ")

    def test_bad_json_duplicate_keys_and_wrong_types(self):
        for raw in (
            b"",
            b"null",
            b"[]",
            b"\xff",
            b"{",
            b'{"version":1,"version":1}',
            "text",
            None,
        ):
            with self.subTest(raw=raw), self.assertRaises(EvidenceError):
                parse_handoff(
                    raw,
                    expected_provenance=self.provenance,
                    expected_producer=self.producer,
                )
        for field, value in (("version", True), ("provenance", []), ("producer", None)):
            with self.subTest(field=field):
                original = self.value[field]
                self.value[field] = value
                with self.assertRaises(EvidenceError):
                    self.parse()
                self.value[field] = original

    def test_missing_unknown_and_review_fields_rejected(self):
        for key in list(self.value):
            original = self.value.pop(key)
            with self.subTest(key=key), self.assertRaises(EvidenceError):
                self.parse()
            self.value[key] = original
        self.value["approved"] = True
        with self.assertRaises(EvidenceError):
            self.parse()

    def test_wrong_role_incomplete_capture(self):
        for field, value in (
            ("role", "reviewer"),
            ("status", "accepted"),
            ("status", "partial"),
        ):
            original = self.value[field]
            self.value[field] = value
            with self.subTest(value=value), self.assertRaises(EvidenceError):
                self.parse()
            self.value[field] = original

    def test_every_provenance_and_producer_field_is_bound(self):
        for section in ("provenance", "producer"):
            for key, original in list(self.value[section].items()):
                self.value[section][key] = (
                    original + 1 if type(original) is int else "f" * len(original)
                )
                with (
                    self.subTest(section=section, key=key),
                    self.assertRaises(EvidenceError),
                ):
                    self.parse()
                self.value[section][key] = original

    def test_typed_identity_validation(self):
        for section, key, bad in (
            ("producer", "turn", True),
            ("producer", "attempt", 0),
            ("producer", "session_id", "a\nb"),
            ("provenance", "repo", None),
            ("provenance", "head_sha", "main"),
            ("provenance", "fixture_sha256", "F" * 64),
        ):
            original = self.value[section][key]
            self.value[section][key] = bad
            with self.subTest(key=key), self.assertRaises(EvidenceError):
                self.parse()
            self.value[section][key] = original

    def test_unsafe_paths_rejected(self):
        for field in ("evidence_root", "manifest_path"):
            for bad in (
                "/tmp/evidence",
                "../evidence",
                "a/../b",
                "a//b",
                "./a",
                "a/",
                "a\\b",
                "a\x00b",
                "",
            ):
                original = self.value[field]
                self.value[field] = bad
                with (
                    self.subTest(field=field, bad=bad),
                    self.assertRaises(EvidenceError),
                ):
                    self.parse()
                self.value[field] = original

    def test_digest_required(self):
        for field in ("manifest_sha256", "inventory_sha256"):
            original = self.value[field]
            for bad in (None, "", "main", "f" * 63, "F" * 64):
                self.value[field] = bad
                with (
                    self.subTest(field=field, bad=bad),
                    self.assertRaises(EvidenceError),
                ):
                    self.parse()
            self.value[field] = original

    def test_export_must_be_completed_and_bound(self):
        cases = [
            replace(self.export, state=state)
            for state in ("queued", "accepted", "failed", "")
        ]
        cases += [
            replace(self.export, fingerprint=""),
            replace(self.export, handoff_sha256="0" * 64),
        ]
        for field in asdict(self.producer):
            original = getattr(self.producer, field)
            changed = original + 1 if type(original) is int else original + "-other"
            cases.append(
                replace(
                    self.export, producer=replace(self.producer, **{field: changed})
                )
            )
        for proof in cases:
            with self.subTest(proof=proof), self.assertRaises(EvidenceError):
                validate_export(self.handoff, proof)

    def test_workspace_manifest_has_separate_eight_mib_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence/42"
            root.mkdir(parents=True)
            (root / "empty.trace").write_bytes(b"")
            entries = [
                {
                    "path": "empty.trace",
                    "size": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                }
            ]
            encoded = json.dumps(
                entries, sort_keys=True, separators=(",", ":")
            ).encode()
            raw = json.dumps({"files": entries}).encode()
            for size in (MAX_HANDOFF_BYTES + 1, MAX_INVENTORY_BYTES):
                manifest = raw + b" " * (size - len(raw))
                (root / "manifest.json").write_bytes(manifest)
                handoff = replace(
                    self.handoff,
                    manifest_sha256=hashlib.sha256(manifest).hexdigest(),
                    inventory_sha256=hashlib.sha256(encoded).hexdigest(),
                )
                with self.subTest(size=size):
                    self.assertEqual(verify_inventory(directory, handoff).files, 1)
            oversized = manifest + b" "
            (root / "manifest.json").write_bytes(oversized)
            handoff = replace(
                handoff, manifest_sha256=hashlib.sha256(oversized).hexdigest()
            )
            with self.assertRaises(EvidenceError):
                verify_inventory(directory, handoff)

    def test_real_inventory_missing_tampered_and_symlink_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence/42"
            root.mkdir(parents=True)
            content = b"example binary capture\x00"
            capture = root / "viewport.png"
            capture.write_bytes(content)
            entries = [
                {
                    "path": "viewport.png",
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            ]
            manifest = json.dumps({"files": entries}).encode()
            (root / "manifest.json").write_bytes(manifest)
            inventory_digest = hashlib.sha256(
                json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            handoff = replace(
                self.handoff,
                manifest_sha256=hashlib.sha256(manifest).hexdigest(),
                inventory_sha256=inventory_digest,
            )
            verified = verify_inventory(directory, handoff)
            self.assertEqual((verified.files, verified.bytes), (1, len(content)))
            capture.write_bytes(b"x" * len(content))
            with self.assertRaises(EvidenceError):
                verify_inventory(directory, handoff)
            capture.unlink()
            with self.assertRaises(EvidenceError):
                verify_inventory(directory, handoff)
            outside = Path(directory) / "outside.png"
            outside.write_bytes(content)
            capture.symlink_to(outside)
            with self.assertRaises(EvidenceError):
                verify_inventory(directory, handoff)
            capture.unlink()
            capture.write_bytes(content)
            (root / "manifest.json").write_bytes(b"tampered")
            with self.assertRaises(EvidenceError):
                verify_inventory(directory, handoff)

    def test_inventory_rejects_symlink_directory_and_unsafe_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence/42"
            root.mkdir(parents=True)
            (root / "link").symlink_to(Path(directory), target_is_directory=True)
            for path in ("../outside", "link/outside", "missing", "manifest.json"):
                entries = [
                    {"path": path, "size": 0, "sha256": hashlib.sha256(b"").hexdigest()}
                ]
                manifest = json.dumps({"files": entries}).encode()
                (root / "manifest.json").write_bytes(manifest)
                handoff = replace(
                    self.handoff,
                    manifest_sha256=hashlib.sha256(manifest).hexdigest(),
                    inventory_sha256=hashlib.sha256(
                        json.dumps(
                            entries, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                )
                with self.subTest(path=path), self.assertRaises(EvidenceError):
                    verify_inventory(directory, handoff)


if __name__ == "__main__":
    unittest.main()
