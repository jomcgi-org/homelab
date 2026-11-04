"""Owner-driven capture-to-review handoff through existing factory/Ember APIs.

Call once per durable owner tick. Pending exports never start an agent. The
caller supplies the current immutable dispatch identities, not model output.
No model verdict or guest-written export claim authorizes a restore.
"""

import json
import re
from dataclasses import asdict

from factory.execution.workspace_handoff import WorkspaceHandoff
from factory.orchestration.browser_evidence import (
    EvidenceError,
    ExportProof,
    Producer,
    Provenance,
    parse_handoff,
    validate_export,
)


async def prepare_browser_review(
    raw: bytes,
    *,
    expected_provenance: Provenance,
    expected_producer: Producer,
    model: str,
    preflight_files: dict,
    transport=None,
) -> dict:
    """Advance export before reserving a node; never dispatch under a DB lock.

    preflight_files must be computed by the owner from repository bytes at the
    exact harness SHA. Other fixture/browser metadata remains declared until
    the mandatory guest preflight measures the restored inputs.
    """
    from factory.execution import model_family
    from factory.execution.transport import EmberVmShimTransport

    handoff = parse_handoff(
        raw,
        expected_provenance=expected_provenance,
        expected_producer=expected_producer,
    )
    if model == "luna":
        raise EvidenceError("capture model cannot approve its own evidence")
    model_family(model)
    transport = transport or EmberVmShimTransport()
    if transport._workload_for(model) != expected_producer.workload:
        raise EvidenceError("reviewer workload cannot restore producer lineage")
    observed = await transport.get_session(expected_producer.session_id)
    identity = {
        "session_id": expected_producer.session_id,
        "lineage_id": expected_producer.lineage_id,
        "workload": expected_producer.workload,
        "principal": expected_producer.principal,
    }
    if any(observed.get(k) != v for k, v in identity.items()):
        raise EvidenceError("producer session identity mismatch")
    if observed.get("state") not in {"destroyed", "expired", "evicted", "failed"}:
        return {"status": "pending", "reason": "producer_not_terminal"}
    receipt = observed.get("workspace_export") or {"status": "unknown"}
    state = receipt.get("status")
    if state == "unknown":
        await transport.request_workspace_export(
            expected_producer.session_id, expected_producer.lineage_id
        )
        return {"status": "pending", "reason": "export_requested"}
    if any(receipt.get(k) != v for k, v in identity.items()) or (
        receipt.get("generation") != observed.get("generation")
        or type(receipt.get("generation")) is not int
    ):
        raise EvidenceError("stale export ownership")
    if state == "pending":
        return {"status": "pending", "reason": "export_pending"}
    if state != "completed":
        raise EvidenceError("workspace export failed or unknown")
    proof = ExportProof(
        expected_producer,
        handoff.handoff_sha256,
        "completed",
        receipt.get("fingerprint"),
    )
    validate_export(handoff, proof)
    required = {
        "projects/monolith/frontend/factory-preview/browser_handoff_check.py",
        "projects/monolith/frontend/factory-preview/sheet_check.py",
        "projects/monolith/factory/orchestration/browser_evidence.py",
    }
    if set(preflight_files) != required or any(
        not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
        for value in preflight_files.values()
    ):
        raise EvidenceError("missing trusted preflight closure")
    payload = {
        "version": 1,
        "handoff": json.loads(raw),
        "expected_workspace_fingerprint": proof.fingerprint,
        "preflight": {
            "harness_sha": expected_provenance.harness_sha,
            "files": preflight_files,
        },
    }
    retention = await transport.lease_capture_evidence(
        expected_producer.session_id, expected_producer.lineage_id, 43200
    )
    context = WorkspaceHandoff(
        expected_producer.lineage_id,
        proof.fingerprint,
        expected_producer.session_id,
        handoff.handoff_sha256,
        preflight_json=json.dumps(payload, sort_keys=True, separators=(",", ":")),
    )
    return {
        "status": "prepared",
        "workspace_handoff": context.to_json(),
        "raw_capture": raw.decode(),
        "expected_provenance": asdict(expected_provenance),
        "expected_producer": asdict(expected_producer),
        "workspace_export": receipt,
        "evidence_retention": retention,
    }
