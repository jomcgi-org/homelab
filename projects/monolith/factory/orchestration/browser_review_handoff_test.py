"""Local handoff preparation: real HTTP adapter, simulated control plane."""

import asyncio
import hashlib
from dataclasses import asdict
import json
import unittest
from unittest.mock import patch, AsyncMock

import httpx

from factory.execution import transport
from factory.execution.workspace_handoff import WorkspaceHandoff
from factory.orchestration.browser_evidence import EvidenceError
from factory.orchestration.browser_evidence_test import BrowserEvidenceTest
from factory.orchestration.browser_review_handoff import prepare_browser_review


class BrowserReviewHandoffTest(unittest.TestCase):
    def setUp(self):
        fixture = BrowserEvidenceTest()
        fixture.setUp()
        self.value = fixture.value
        self.provenance, self.producer = fixture.provenance, fixture.producer
        self.producer = self.producer.__class__(
            **{
                **asdict(self.producer),
                "session_id": "s-producer",
                "workload": transport.EmberVmShimTransport()._workload_for("sol"),
            }
        )
        self.value["producer"] = asdict(self.producer)
        self.receipt = {"status": "unknown"}
        self.generation = 3
        self.requested = 0
        self.created = []
        self.requests = []
        self.state = "destroyed"
        self.preflight_files = {
            "projects/monolith/frontend/factory-preview/browser_handoff_check.py": "1"
            * 64,
            "projects/monolith/frontend/factory-preview/sheet_check.py": "2" * 64,
            "projects/monolith/factory/orchestration/browser_evidence.py": "3" * 64,
        }
        self.restore_ok = True
        self.lineage_override = None
        original_client = httpx.AsyncClient

        def client(**kwargs):
            return original_client(transport=httpx.MockTransport(self.handle), **kwargs)

        self.patches = [
            patch.object(transport, "EMBERVM_URL", "http://ember.test"),
            patch.object(transport, "auth_headers", return_value={}),
            patch.object(transport.httpx, "AsyncClient", side_effect=client),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def identity(self):
        return {
            "session_id": self.producer.session_id,
            "lineage_id": self.lineage_override or self.producer.lineage_id,
            "workload": self.producer.workload,
            "principal": self.producer.principal,
            "generation": self.generation,
        }

    def complete(self):
        self.receipt = {
            **self.identity(),
            "status": "completed",
            "fingerprint": "e" * 64,
        }

    def handle(self, request):
        self.requests.append((request.method, request.url.path))
        path = request.url.path
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    **self.identity(),
                    "state": self.state,
                    "workspace_export": self.receipt,
                },
            )
        if path.endswith("/evidence-retention"):
            body = json.loads(request.content)
            self.assertEqual(body["operation"], "lease")
            self.assertLessEqual(body["lease_ms"], 43200000)
            return httpx.Response(
                200,
                json={
                    "status": "retained",
                    "policy": {"lineage_id": self.producer.lineage_id},
                },
            )
        if path.endswith("/workspace-export"):
            self.assertEqual(
                json.loads(request.content), {"lineage_id": self.producer.lineage_id}
            )
            self.requested += 1
            self.receipt = {**self.identity(), "status": "pending"}
            return httpx.Response(202, json=self.receipt)
        self.created.append(json.loads(request.content))
        return httpx.Response(
            201,
            json={
                "session_id": "s-reviewer",
                "session_token": "test-only-token",
                "lineage_id": self.producer.lineage_id,
                "restored": self.restore_ok,
                "workspace_fingerprint": "e" * 64 if self.restore_ok else "f" * 64,
            },
        )

    def prepare(self):
        return asyncio.run(
            prepare_browser_review(
                json.dumps(self.value).encode(),
                expected_provenance=self.provenance,
                expected_producer=self.producer,
                model="sol",
                preflight_files=self.preflight_files,
            )
        )

    def test_pending_export_then_prepared_handoff_never_creates_session(self):
        self.assertEqual(
            self.prepare(), {"status": "pending", "reason": "export_requested"}
        )
        self.assertEqual(
            self.prepare(), {"status": "pending", "reason": "export_pending"}
        )
        self.assertEqual(self.requested, 1)
        self.assertEqual(self.created, [])
        self.complete()
        result = self.prepare()
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(result["workspace_export"], self.receipt)
        self.assertEqual(result["raw_capture"], json.dumps(self.value))
        self.assertEqual(result["expected_provenance"], asdict(self.provenance))
        self.assertEqual(result["expected_producer"], asdict(self.producer))
        self.assertEqual(
            result["evidence_retention"],
            {"status": "retained", "policy": {"lineage_id": self.producer.lineage_id}},
        )
        digest = hashlib.sha256(
            json.dumps(self.value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        payload = {
            "version": 1,
            "handoff": self.value,
            "expected_workspace_fingerprint": "e" * 64,
            "preflight": {
                "harness_sha": self.provenance.harness_sha,
                "files": self.preflight_files,
            },
        }
        self.assertEqual(
            WorkspaceHandoff.from_json(result["workspace_handoff"]),
            WorkspaceHandoff(
                lineage_id=self.producer.lineage_id,
                expected_fingerprint="e" * 64,
                producer_ember_session_id=self.producer.session_id,
                handoff_sha256=digest,
                preflight_json=json.dumps(
                    payload, sort_keys=True, separators=(",", ":")
                ),
            ),
        )
        self.assertEqual(self.created, [])
        self.assertEqual(
            [
                path.rsplit("/", 1)[-1]
                for method, path in self.requests
                if method == "POST"
            ],
            [
                "workspace-export",
                "evidence-retention",
            ],
        )

    def test_nonterminal_producer_never_creates_session(self):
        self.state = "running"
        self.assertEqual(
            self.prepare(), {"status": "pending", "reason": "producer_not_terminal"}
        )
        self.assertEqual(self.created, [])
        self.assertEqual(self.requested, 0)

    def test_failed_export_never_dispatches(self):
        self.receipt = {
            **self.identity(),
            "status": "failed",
            "reason": "export_failed",
        }
        with self.assertRaises(EvidenceError):
            self.prepare()
        self.assertEqual(self.created, [])
        self.assertEqual(self.requested, 0)

    def test_wrong_lineage_and_stale_generation(self):
        self.complete()
        self.generation += 1
        with self.assertRaises(EvidenceError):
            self.prepare()
        self.lineage_override = "wrong-lineage"
        with self.assertRaises(EvidenceError):
            self.prepare()
        self.assertEqual(self.created, [])

    def test_stale_capture_rejected_before_network(self):
        self.value["provenance"]["head_sha"] = "0" * 40
        with self.assertRaises(EvidenceError):
            self.prepare()
        self.assertEqual(self.requested, 0)
        self.assertEqual(self.created, [])
        self.assertEqual(self.requests, [])

    def test_restore_mismatch_never_binds_review(self):
        self.restore_ok = False
        with self.assertRaises(transport.EmberVMTransportError):
            asyncio.run(
                transport.EmberVmShimTransport().create_session(
                    restore_from=self.producer.lineage_id,
                    model="sol",
                    expected_workspace_fingerprint="e" * 64,
                )
            )
        self.assertEqual(
            self.created,
            [
                {
                    "restore_lineage": self.producer.lineage_id,
                    "expected_workspace_fingerprint": "e" * 64,
                }
            ],
        )

    def test_existing_binding_cannot_substitute_another_lineage(self):
        client = transport.EmberVmShimTransport()
        bound = transport.EmberSession(
            "s-reviewer", "test-only", None, lineage_id="wrong-lineage"
        )
        client.get_session = AsyncMock(
            return_value={
                "session_id": "s-reviewer",
                "lineage_id": "wrong-lineage",
                "workspace_fingerprint": "e" * 64,
            }
        )
        with self.assertRaises(transport.EmberVMTransportError):
            asyncio.run(
                client._deliver(
                    bound,
                    None,
                    "review",
                    model="sol",
                    restore_from=self.producer.lineage_id,
                    expected_workspace_fingerprint="e" * 64,
                )
            )
        self.assertEqual(self.created, [])

    def test_invalid_expected_fingerprint_never_creates(self):
        with self.assertRaises(transport.EmberVMTransportError):
            asyncio.run(
                transport.EmberVmShimTransport().create_session(
                    restore_from=self.producer.lineage_id,
                    expected_workspace_fingerprint="bad",
                )
            )
        self.assertEqual(self.created, [])


if __name__ == "__main__":
    unittest.main()
