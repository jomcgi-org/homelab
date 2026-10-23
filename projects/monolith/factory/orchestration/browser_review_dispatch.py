"""Prepare browser review dependencies before reserving a factory attempt.

Only source identity and producer identity are server observations here.
Browser/fixture digests remain capture claims until mandatory guest preflight.
No session is started and no factory control lock is held during CP/GitHub I/O.
"""

import asyncio
import base64
import hashlib
import json
from dataclasses import asdict

from core.db import get_engine
from sqlmodel import Session, select

from factory.execution.models import AgentSession, AgentTurn
from factory.execution.transport import EmberVmShimTransport
from factory.orchestration.browser_evidence import EvidenceError, Producer, Provenance
from factory.orchestration.models import SwarmNodeRun

PREFLIGHT_PATHS = (
    "projects/monolith/frontend/factory-preview/browser_handoff_check.py",
    "projects/monolith/frontend/factory-preview/sheet_check.py",
    "projects/monolith/factory/orchestration/browser_evidence.py",
)


def verify_review_preflight(
    context_json: str | None, usage_json: str | None, expected_digest: str
) -> None:
    """Require shim-owned verification, not a reviewer-written approval claim."""
    from factory.execution.workspace_handoff import WorkspaceHandoff

    if context_json is None:
        raise EvidenceError("review has no trusted workspace handoff")
    context = WorkspaceHandoff.from_json(context_json)
    if context.handoff_sha256 != expected_digest or context.preflight_json is None:
        raise EvidenceError("review handoff contract mismatch")
    declared = json.loads(context.preflight_json)["handoff"]
    proof = json.loads(usage_json or "{}").get("workspace_preflight")
    expected = {
        "status": "ok",
        "handoff_sha256": expected_digest,
        "manifest_sha256": declared["manifest_sha256"],
        "inventory_sha256": declared["inventory_sha256"],
        **{
            key: declared["provenance"][key]
            for key in ("base_sha", "head_sha", "harness_sha")
        },
    }
    if not isinstance(proof, dict) or any(
        proof.get(key) != value for key, value in expected.items()
    ):
        raise EvidenceError("missing or mismatched authoritative workspace preflight")
    if (
        type(proof.get("files")) is not int
        or proof["files"] < 1
        or type(proof.get("bytes")) is not int
        or proof["bytes"] < 0
    ):
        raise EvidenceError("invalid authoritative workspace inventory counts")


def verify_stored_review_preflight(
    path: str, artifact_blob: bytes | None, digest: str
) -> None:
    """Bind both completion readers to one exact durable review turn.

    Artifact paths are globally scoped to factory task/node/attempt identities.
    Ambiguous paths cannot borrow another session's successful preflight.
    """
    if artifact_blob is None:
        raise EvidenceError("browser review requires stored artifact bytes")
    with Session(get_engine()) as db:
        turns = db.exec(
            select(AgentTurn).where(AgentTurn.artifact_path == path).limit(2)
        ).all()
        if len(turns) != 1:
            raise EvidenceError("browser review artifact path is missing or ambiguous")
        turn = turns[0]
        if (
            turn.seq != 1
            or turn.artifact_outcome != "ok"
            or turn.artifact_blob is None
            or bytes(turn.artifact_blob) != bytes(artifact_blob)
        ):
            raise EvidenceError("browser review artifact bytes or turn mismatch")
        owner = db.get(AgentSession, turn.session_id)
        if owner is None:
            raise EvidenceError("browser review artifact has no owner")
        parts = owner.local_session_id.split(":")
        if (
            len(parts) != 4
            or parts[0] != "factory"
            or not parts[2].startswith("review_")
            or owner.node_key != parts[2]
            or str(owner.node_attempt) != parts[3]
            or path != f".factory/{parts[1]}/{parts[2]}-{parts[3]}.json"
        ):
            raise EvidenceError("browser review artifact owner identity mismatch")
        verify_review_preflight(owner.workspace_handoff_json, turn.usage_json, digest)


def capture_dependency(node: dict, runs: list[dict]) -> dict | None:
    """A review may consume one exact successful capture dependency."""
    if not node["node_key"].startswith("review_"):
        return None
    captures = []
    for dependency in node.get("deps", []):
        succeeded = [
            r
            for r in runs
            if r["node_key"] == dependency and r["status"] == "succeeded"
        ]
        if not succeeded:
            continue
        run = max(succeeded, key=lambda r: r.get("attempt", 0))
        outcome = json.loads(run.get("outcome_json") or "{}")
        artifact = outcome.get("value") or outcome.get("artifact") or {}
        if "browser_evidence" in artifact:
            if artifact.get("status") != "complete":
                raise EvidenceError("browser capture dependency is not complete")
            captures.append(run)
    if len(captures) > 1:
        raise EvidenceError("review must name exactly one browser capture dependency")
    return captures[0] if captures else None


def _producer_observation(task_id: str, run: dict) -> dict:
    """Read the exact persisted run/session/turn, never guest identity fields."""
    with Session(get_engine()) as db:
        stored = db.get(SwarmNodeRun, run["id"])
        if (
            stored is None
            or stored.task_id != task_id
            or stored.status != "succeeded"
            or stored.session_id != run["session_id"]
        ):
            raise EvidenceError("capture run binding changed")
        owner = db.get(AgentSession, stored.session_id)
        turn = db.exec(
            select(AgentTurn).where(
                AgentTurn.session_id == stored.session_id, AgentTurn.seq == 1
            )
        ).first()
        if (
            owner is None
            or turn is None
            or owner.node_key != stored.node_key
            or owner.node_attempt != stored.attempt
            or owner.local_session_id
            != f"factory:{task_id}:{stored.node_key}:{stored.attempt}"
        ):
            raise EvidenceError("capture producer binding mismatch")
        receipt = json.loads(turn.usage_json or "{}").get("browser_capture_producer")
        expected = {
            "agent_session_id": owner.id,
            "node_id": stored.node_key,
            "attempt": stored.attempt,
            "turn": turn.seq,
        }
        if (
            not isinstance(receipt, dict)
            or any(receipt.get(key) != value for key, value in expected.items())
            or not isinstance(receipt.get("session_id"), str)
            or not receipt["session_id"]
            or not isinstance(receipt.get("lineage_id"), str)
            or not receipt["lineage_id"]
            or owner.capture_evidence_ttl_seconds is None
        ):
            raise EvidenceError("capture producer has no trusted completion identity")
        if owner.ember_session_id is not None and (
            owner.ember_session_id != receipt["session_id"]
            or owner.ember_lineage_id != receipt["lineage_id"]
        ):
            raise EvidenceError("capture producer binding changed after completion")
        outcome = json.loads(stored.outcome_json or "{}")
        artifact = outcome.get("value") or outcome.get("artifact") or {}
        additional_turn = db.exec(
            select(AgentTurn.id).where(
                AgentTurn.session_id == stored.session_id, AgentTurn.seq != turn.seq
            )
        ).first()
        if (
            additional_turn is not None
            or turn.artifact_outcome != "ok"
            or turn.artifact_blob is None
            or json.loads(bytes(turn.artifact_blob)) != artifact
        ):
            raise EvidenceError(
                "capture outcome is not the exact single producer turn artifact"
            )
        return {
            "run_id": str(stored.id),
            "node_id": stored.node_key,
            "attempt": stored.attempt,
            "session_id": receipt["session_id"],
            "lineage_id": receipt["lineage_id"],
            "turn": turn.seq,
            "model": owner.model,
            "artifact": artifact,
        }


def prepare_dependency(
    task: dict,
    node: dict,
    run: dict,
    model: str,
    *,
    github_get,
    delivery_identity_matches,
) -> dict:
    """Conductor tick adapter. Pending exports leave the reviewer ready."""
    observed = _producer_observation(task["id"], run)
    artifact = observed.pop("artifact")
    number = artifact.get("pr_number")
    if type(number) is not int or number < 1:
        raise EvidenceError("browser capture has no delivery PR")
    pull = github_get(task["repo"], f"pulls/{number}")
    if pull.get("state") != "open" or not delivery_identity_matches(task, pull):
        raise EvidenceError("browser capture PR does not match task")
    head, target = pull["head"]["sha"], pull["base"]["sha"]
    comparison = github_get(task["repo"], f"compare/{target}...{head}")
    base = comparison["merge_base_commit"]["sha"]
    raw_value = json.loads(json.dumps(artifact["browser_evidence"]))
    # These runtime digests are declared, not trusted measurements. The pinned
    # preflight remeasures their inventory-bound inputs before any model call.
    declared = raw_value["provenance"]
    provenance = Provenance(
        **{
            **declared,
            "repo": task["repo"],
            "base_sha": base,
            "target_sha": target,
            "head_sha": head,
            "harness_sha": head,
        }
    )
    provenance.validate()
    files = {}
    for path in PREFLIGHT_PATHS:
        content = github_get(task["repo"], f"contents/{path}?ref={head}")
        if content.get("encoding") != "base64" or content.get("type") != "file":
            raise EvidenceError("trusted preflight source unavailable")
        files[path] = hashlib.sha256(
            base64.b64decode(content["content"], validate=False)
        ).hexdigest()
    transport = EmberVmShimTransport()

    async def prepare():
        from factory.orchestration.browser_review_handoff import prepare_browser_review

        cp = await transport.get_session(observed["session_id"])
        workload = transport._workload_for(observed.pop("model"))
        if (
            cp.get("session_id") != observed["session_id"]
            or cp.get("lineage_id") != observed["lineage_id"]
            or cp.get("workload") != workload
        ):
            raise EvidenceError("capture control-plane identity mismatch")
        producer = Producer(
            **observed, workload=workload, principal=cp.get("principal")
        )
        producer.validate()
        # Guest output cannot authenticate its CP identity. Keep the original
        # artifact untouched and bind this prepared copy to server observations.
        raw_value["producer"] = asdict(producer)
        return await prepare_browser_review(
            json.dumps(raw_value, sort_keys=True, separators=(",", ":")).encode(),
            expected_provenance=provenance,
            expected_producer=producer,
            model=model,
            transport=transport,
            preflight_files=files,
        )

    return asyncio.run(prepare())
