"""Local handoff integration: real HTTP adapter, simulated control plane/queue."""

import asyncio
from dataclasses import asdict
import json
import unittest
from unittest.mock import patch, AsyncMock

import httpx

from factory.execution import transport
from factory.orchestration.browser_evidence import EvidenceError
from factory.orchestration.browser_evidence_test import BrowserEvidenceTest
from factory.orchestration.browser_review_handoff import advance_browser_review


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
        self.starts = []
        self.created = []
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
        path = request.url.path
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    **self.identity(),
                    "state": "destroyed",
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

    def dispatch(self, key, prompt, model, repo, branch, **kwargs):
        context = kwargs["workspace_handoff"]
        restored = asyncio.run(
            transport.EmberVmShimTransport().create_session(
                restore_from=context.lineage_id,
                model=model,
                expected_workspace_fingerprint=context.expected_fingerprint,
            )
        )
        self.assertNotEqual(restored.session_id, context.producer_ember_session_id)
        self.assertTrue(restored.restored)
        self.assertIn("Independently review", prompt)
        self.assertEqual(branch, self.provenance.head_sha)
        self.starts.append((key, context))
        return 43

    def advance(self):
        return asyncio.run(
            advance_browser_review(
                json.dumps(self.value).encode(),
                expected_provenance=self.provenance,
                expected_producer=self.producer,
                task_id="task42",
                reviewer_node="review_browser",
                reviewer_attempt=1,
                workflow_id="workflow42",
                model="sol",
                dispatch=self.dispatch,
                preflight_files={
                    "projects/monolith/frontend/factory-preview/browser_handoff_check.py": "1"
                    * 64,
                    "projects/monolith/frontend/factory-preview/sheet_check.py": "2"
                    * 64,
                    "projects/monolith/factory/orchestration/browser_evidence.py": "3"
                    * 64,
                },
            )
        )

    def test_pending_export_then_exact_independent_restore(self):
        self.assertEqual(self.advance()["reason"], "export_requested")
        self.assertEqual(self.advance()["reason"], "export_pending")
        self.assertEqual(self.requested, 1)
        self.assertEqual(self.starts, [])
        self.complete()
        result = self.advance()
        self.assertEqual(result["status"], "review_queued")
        self.assertEqual(len(self.starts), 1)
        self.assertEqual(
            self.created,
            [
                {
                    "restore_lineage": self.producer.lineage_id,
                    "expected_workspace_fingerprint": "e" * 64,
                }
            ],
        )

    def test_failed_export_never_dispatches(self):
        self.receipt = {
            **self.identity(),
            "status": "failed",
            "reason": "export_failed",
        }
        with self.assertRaises(EvidenceError):
            self.advance()
        self.assertEqual(self.starts, [])
        self.assertEqual(self.requested, 0)

    def test_wrong_lineage_and_stale_generation(self):
        self.complete()
        self.generation += 1
        with self.assertRaises(EvidenceError):
            self.advance()
        self.lineage_override = "wrong-lineage"
        with self.assertRaises(EvidenceError):
            self.advance()
        self.assertEqual(self.starts, [])

    def test_stale_capture_rejected_before_network(self):
        self.value["provenance"]["head_sha"] = "0" * 40
        with self.assertRaises(EvidenceError):
            self.advance()
        self.assertEqual(self.requested, 0)
        self.assertEqual(self.starts, [])

    def test_restore_mismatch_never_binds_review(self):
        self.complete()
        self.restore_ok = False
        with self.assertRaises(transport.EmberVMTransportError):
            self.advance()
        self.assertEqual(self.starts, [])

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
