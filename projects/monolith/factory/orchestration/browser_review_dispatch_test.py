"""Factory readiness and authoritative completion boundaries, without live I/O."""

import base64
import json
import unittest
from dataclasses import asdict, replace
from unittest.mock import patch

from factory.execution.workspace_handoff import WorkspaceHandoff
from factory.orchestration import browser_review_dispatch as dispatch
from factory.orchestration.browser_evidence import EvidenceError
from factory.orchestration.browser_evidence_test import BrowserEvidenceTest


class BrowserReviewDispatchTest(unittest.TestCase):
    def setUp(self):
        fixture = BrowserEvidenceTest()
        fixture.setUp()
        self.value, self.producer, self.provenance = (
            fixture.value,
            fixture.producer,
            fixture.provenance,
        )
        self.run = {
            "id": 1,
            "node_key": "implement_browser_capture_mobile",
            "attempt": 1,
            "session_id": 4,
            "status": "succeeded",
            "outcome_json": json.dumps(
                {"value": {"status": "complete", "browser_evidence": self.value}}
            ),
        }
        self.node = {
            "node_key": "review_browser",
            "deps": ["implement_browser_capture_mobile"],
        }

    def test_completed_capture_identity_survives_real_guest_cleanup(self):
        import tempfile
        from pathlib import Path

        from sqlmodel import Session, SQLModel, create_engine

        from factory.execution import store
        from factory.execution.models import (
            AgentCapacityPool,
            AgentCapacityReservation,
            AgentResultReceipt,
            AgentSession,
            AgentTurn,
            PendingMessage,
        )
        from factory.execution.transport import Turn
        from factory.orchestration.models import SwarmNodeRun, SwarmTask

        with tempfile.TemporaryDirectory() as directory:
            engine = create_engine(
                f"sqlite:///{Path(directory) / 'capture.db'}",
                execution_options={
                    "schema_translate_map": {"agent_sessions": None, "swarm": None}
                },
            )
            SQLModel.metadata.create_all(
                engine,
                tables=[
                    model.__table__
                    for model in (
                        AgentSession,
                        AgentTurn,
                        PendingMessage,
                        AgentCapacityPool,
                        AgentCapacityReservation,
                        AgentResultReceipt,
                        SwarmTask,
                        SwarmNodeRun,
                    )
                ],
            )
            node = "implement_browser_capture_mobile"
            artifact = {
                "pr_number": 1,
                "status": "complete",
                "browser_evidence": self.value,
            }
            with Session(engine) as db:
                owner = store.create_session(
                    db,
                    f"factory:task:{node}:1",
                    "guest",
                    "main",
                    "luna",
                    workflow_id="capture-workflow",
                    node_key=node,
                    node_attempt=1,
                    capture_evidence_ttl_seconds=259200,
                )
                sid = owner.id
                store.set_ember_session(
                    db, sid, "trusted-guest", "token", None, "trusted-lineage"
                )
                store.create_pending_message(db, sid, "capture")
                db.add(SwarmTask(id="task", task_text="capture", conductor_model="sol"))
                run = SwarmNodeRun(
                    task_id="task",
                    node_key=node,
                    attempt=1,
                    session_id=sid,
                    status="succeeded",
                    outcome_json=json.dumps({"value": artifact}),
                )
                db.add(run)
                db.commit()
                run_id = run.id
            turn = Turn(
                "done",
                "completed",
                "end_turn",
                False,
                [],
                1,
                "capture-cli",
                {"browser_capture_producer": {"session_id": "forged"}},
                0.0,
                1,
                [],
            )
            turn = turn._replace(
                artifact={
                    "path": "capture.json",
                    "outcome": "ok",
                    "content_b64": base64.b64encode(
                        json.dumps(artifact).encode()
                    ).decode(),
                }
            )
            with patch.object(store, "get_engine", return_value=engine):
                store.persist_turn_from_pending_sync(
                    sid, 1, "capture", turn, "", "idle"
                )
            with Session(engine) as db:
                claim = store.begin_guest_cleanup(
                    db, sid, "trusted-guest", "capture-workflow"
                )
                self.assertTrue(
                    store.finish_guest_cleanup(
                        db, sid, "trusted-guest", "capture-workflow", claim["claim_id"]
                    )
                )
                self.assertIsNone(db.get(AgentSession, sid).ember_session_id)
                self.assertIsNone(db.get(AgentSession, sid).ember_lineage_id)
            with patch.object(dispatch, "get_engine", return_value=engine):
                observed = dispatch._producer_observation(
                    "task", {"id": run_id, "session_id": sid}
                )
            self.assertEqual(observed["session_id"], "trusted-guest")
            self.assertEqual(observed["lineage_id"], "trusted-lineage")
            self.assertEqual(observed["artifact"], artifact)
            with Session(engine) as db:
                db.add(
                    AgentTurn(
                        session_id=sid,
                        seq=2,
                        prompt="unexpected continuation",
                        result_text="done",
                    )
                )
                db.commit()
            with (
                patch.object(dispatch, "get_engine", return_value=engine),
                self.assertRaises(EvidenceError),
            ):
                dispatch._producer_observation(
                    "task", {"id": run_id, "session_id": sid}
                )
            engine.dispose()

    def test_stored_preflight_binds_unique_path_bytes_turn_and_owner(self):
        import tempfile
        from pathlib import Path

        from sqlmodel import Session, SQLModel, create_engine

        from factory.execution.models import AgentSession, AgentTurn
        from factory.orchestration import node_workflows

        prepared, _ = self.prepare()
        context = WorkspaceHandoff.from_json(prepared["workspace_handoff"])
        proof = {
            "status": "ok",
            "handoff_sha256": context.handoff_sha256,
            "manifest_sha256": self.value["manifest_sha256"],
            "inventory_sha256": self.value["inventory_sha256"],
            "files": 3,
            "bytes": 90,
            **{
                key: getattr(self.provenance, key)
                for key in ("base_sha", "head_sha", "harness_sha")
            },
        }
        path = ".factory/task/review_browser-1.json"
        blob = json.dumps(
            {
                "verdict": "approve",
                "browser_evidence_handoff_sha256": context.handoff_sha256,
            }
        ).encode()
        schema = {
            "type": "object",
            "properties": {
                "verdict": {"enum": ["approve"]},
                "browser_evidence_handoff_sha256": {"enum": [context.handoff_sha256]},
            },
        }
        for case in (
            "valid",
            "digest",
            "bytes",
            "owner",
            "other_task",
            "multiple",
            "turn",
            "missing_proof",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                engine = create_engine(
                    f"sqlite:///{Path(directory) / 'review.db'}",
                    execution_options={
                        "schema_translate_map": {"agent_sessions": None}
                    },
                )
                SQLModel.metadata.create_all(
                    engine, tables=[AgentSession.__table__, AgentTurn.__table__]
                )
                with Session(engine) as db:
                    db.add(
                        AgentSession(
                            id=1,
                            local_session_id=(
                                "factory:other:review_browser:1"
                                if case == "other_task"
                                else "factory:task:review_browser:1"
                            ),
                            node_key="review_other"
                            if case == "owner"
                            else "review_browser",
                            node_attempt=1,
                            workspace="guest",
                            branch="main",
                            workspace_handoff_json=context.to_json(),
                        )
                    )
                    db.add(
                        AgentTurn(
                            session_id=1,
                            seq=2 if case == "turn" else 1,
                            prompt="review",
                            result_text="done",
                            artifact_path=path,
                            artifact_blob=blob,
                            artifact_outcome="ok",
                            usage_json="{}"
                            if case == "missing_proof"
                            else json.dumps({"workspace_preflight": proof}),
                        )
                    )
                    if case == "multiple":
                        db.add(
                            AgentTurn(
                                session_id=1,
                                seq=2,
                                prompt="duplicate",
                                result_text="done",
                                artifact_path=path,
                                artifact_blob=blob,
                                artifact_outcome="ok",
                            )
                        )
                    db.commit()
                with (
                    patch.object(dispatch, "get_engine", return_value=engine),
                    patch("core.db.get_engine", return_value=engine),
                ):
                    if case == "valid":
                        dispatch.verify_stored_review_preflight(
                            path, blob, context.handoff_sha256
                        )
                        self.assertEqual(
                            node_workflows._read_turn_artifact.__wrapped__(
                                1, 1, path, schema
                            )["status"],
                            "ok",
                        )
                    else:
                        with self.assertRaises(EvidenceError):
                            dispatch.verify_stored_review_preflight(
                                path,
                                b"wrong" if case == "bytes" else blob,
                                "f" * 64
                                if case == "digest"
                                else context.handoff_sha256,
                            )
                        if case not in ("bytes", "digest"):
                            self.assertEqual(
                                node_workflows._read_turn_artifact.__wrapped__(
                                    1, 2 if case == "turn" else 1, path, schema
                                )["status"],
                                "invalid",
                            )
                engine.dispose()

    def test_capture_dependency_is_explicit_and_unique(self):
        self.assertEqual(dispatch.capture_dependency(self.node, [self.run]), self.run)
        self.assertIsNone(
            dispatch.capture_dependency(
                {**self.node, "node_key": "implement_a"}, [self.run]
            )
        )
        self.assertIsNone(
            dispatch.capture_dependency(self.node, [{**self.run, "status": "failed"}])
        )
        with self.assertRaises(EvidenceError):
            dispatch.capture_dependency(
                {
                    **self.node,
                    "deps": [
                        "implement_browser_capture_mobile",
                        "implement_browser_capture_desktop",
                    ],
                },
                [
                    self.run,
                    {**self.run, "node_key": "implement_browser_capture_desktop"},
                ],
            )

    def test_ordinary_correction_cannot_select_browser_handoff(self):
        for declaration in ({}, self.value):
            with self.subTest(declaration=declaration):
                correction = {
                    **self.run,
                    "node_key": "correct_1",
                    "outcome_json": json.dumps(
                        {
                            "value": {
                                "status": "complete",
                                "browser_evidence": declaration,
                            }
                        }
                    ),
                }
                self.assertIsNone(
                    dispatch.capture_dependency(
                        {"node_key": "review_1", "deps": ["correct_1"]},
                        [correction],
                    )
                )

    def test_capture_contract_cannot_be_omitted_by_its_artifact(self):
        for artifact in (
            {"status": "complete"},
            {"status": "complete", "browser_evidence": None},
        ):
            with self.subTest(artifact=artifact), self.assertRaises(EvidenceError):
                dispatch.capture_dependency(
                    self.node,
                    [{**self.run, "outcome_json": json.dumps({"value": artifact})}],
                )

    def test_ordinary_correction_does_not_mask_required_capture(self):
        correction = {**self.run, "node_key": "correct_1"}
        self.assertEqual(
            dispatch.capture_dependency(
                {**self.node, "deps": [*self.node["deps"], "correct_1"]},
                [correction, self.run],
            ),
            self.run,
        )

    def prepare(
        self,
        *,
        stale_head=False,
        pending=False,
        forged_producer=False,
        omitted_producer=False,
    ):
        raw = json.loads(json.dumps(self.value))
        if omitted_producer:
            raw.pop("producer")
        if forged_producer:
            raw["producer"] = {"session_id": "guest-claim-not-authenticated"}
        if stale_head:
            raw["provenance"]["head_sha"] = "f" * 40
        observed = {
            **asdict(self.producer),
            "model": "luna",
            "artifact": {"pr_number": 1, "browser_evidence": raw},
        }
        observed.pop("principal")
        observed.pop("workload")
        producer = self.producer

        class Transport:
            async def lease_capture_evidence(self, sid, lineage, seconds):
                return {"status": "retained", "policy": {"lease_ms": seconds * 1000}}

            def _workload_for(self, model):
                return producer.workload

            async def get_session(self, sid):
                identity = {
                    key: getattr(producer, key)
                    for key in ("session_id", "lineage_id", "workload", "principal")
                }
                return {
                    **identity,
                    "state": "destroyed",
                    "generation": 2,
                    "workspace_export": {
                        **identity,
                        "generation": 2,
                        "status": "pending" if pending else "completed",
                        "fingerprint": "e" * 64,
                    },
                }

        calls = []

        def github(repo, path):
            calls.append(path)
            if path.startswith("pulls/"):
                return {
                    "state": "open",
                    "head": {"sha": self.provenance.head_sha},
                    "base": {"sha": self.provenance.target_sha},
                }
            if path.startswith("compare/"):
                return {"merge_base_commit": {"sha": self.provenance.base_sha}}
            return {
                "encoding": "base64",
                "type": "file",
                "content": base64.b64encode(b"trusted source bytes").decode(),
            }

        with (
            patch.object(dispatch, "_producer_observation", return_value=observed),
            patch.object(dispatch, "EmberVmShimTransport", return_value=Transport()),
        ):
            result = dispatch.prepare_dependency(
                {"id": "task", "repo": self.provenance.repo},
                self.node,
                self.run,
                "sol",
                github_get=github,
                delivery_identity_matches=lambda *args: True,
            )
        return result, calls

    def test_server_source_identity_and_preflight_closure_are_pinned(self):
        result, calls = self.prepare()
        self.assertEqual(result["status"], "prepared")
        context = WorkspaceHandoff.from_json(result["workspace_handoff"])
        preflight = json.loads(context.preflight_json)
        self.assertEqual(
            set(preflight["preflight"]["files"]), set(dispatch.PREFLIGHT_PATHS)
        )
        self.assertTrue(
            all(
                f"?ref={self.provenance.head_sha}" in path
                for path in calls
                if path.startswith("contents/")
            )
        )
        with self.assertRaises(EvidenceError):
            self.prepare(stale_head=True)

    def test_guest_producer_identity_is_replaced_by_server_observation(self):
        result, _ = self.prepare(forged_producer=True)
        self.assertEqual(
            json.loads(result["raw_capture"])["producer"], asdict(self.producer)
        )

    def test_capture_declaration_needs_no_guest_producer_identity(self):
        result, _ = self.prepare(omitted_producer=True)
        self.assertEqual(
            json.loads(result["raw_capture"])["producer"], asdict(self.producer)
        )

    def test_pending_export_does_not_prepare_a_start(self):
        result, _ = self.prepare(pending=True)
        self.assertEqual(result["status"], "pending")
        self.assertNotIn("workspace_handoff", result)

    def test_preflight_payload_cannot_be_rebound_to_other_restore(self):
        result, _ = self.prepare()
        context = WorkspaceHandoff.from_json(result["workspace_handoff"])
        for name, value in (
            ("lineage_id", "another-lineage"),
            ("producer_ember_session_id", "another-session"),
            ("expected_fingerprint", "f" * 64),
            ("handoff_sha256", "f" * 64),
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                replace(context, **{name: value}).to_json()

    def test_review_needs_authoritative_preflight_not_model_approval(self):
        prepared, _ = self.prepare()
        context = WorkspaceHandoff.from_json(prepared["workspace_handoff"])
        proof = {
            "status": "ok",
            "handoff_sha256": context.handoff_sha256,
            "manifest_sha256": self.value["manifest_sha256"],
            "inventory_sha256": self.value["inventory_sha256"],
            "files": 3,
            "bytes": 90,
            **{
                key: getattr(self.provenance, key)
                for key in ("base_sha", "head_sha", "harness_sha")
            },
        }
        dispatch.verify_review_preflight(
            context.to_json(),
            json.dumps({"workspace_preflight": proof}),
            context.handoff_sha256,
        )
        for bad in (
            {"verdict": "approve"},
            {"workspace_preflight": {**proof, "head_sha": "f" * 40}},
            {"workspace_preflight": {**proof, "files": 0}},
        ):
            with self.assertRaises(EvidenceError):
                dispatch.verify_review_preflight(
                    context.to_json(), json.dumps(bad), context.handoff_sha256
                )


if __name__ == "__main__":
    unittest.main()
