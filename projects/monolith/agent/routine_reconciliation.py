"""Explicit operator reconciliation of ceased KG attempts, never automatic retry.

The trusted caller obtains fresh authoritative Ember GET evidence. This module
has no HTTP/MCP surface and cannot infer cessation from a timeout or missing row.
Old AgentTurns remain unknown and their sessions remain failed and unsendable.
"""

from datetime import datetime, timezone
import hashlib
import json
import re

from sqlalchemy import text
from sqlmodel import Session, select

from agent.routine_reconciliation_models import RoutineReconciliation
from agent_sessions.api import (
    KG_NODE_KEY,
    confirm_reconciled_guest_cessation,
    lock_capacity_pool,
    lock_cessation_session,
)
from core.db import get_engine
from knowledge.api import EXTRACTION_VERSION
from shared.invocation_outcomes import UNKNOWN_INVOCATION

MAX_EVIDENCE_AGE_SECONDS = 60


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _table(db, schema, name):
    return name if db.bind.dialect.name == "sqlite" else f"{schema}.{name}"


def _rows(db, sql, params):
    return [dict(row) for row in db.execute(text(sql), params).mappings()]


def _payload(value):
    value = json.loads(value) if isinstance(value, str) else value
    if not isinstance(value, dict):
        raise ValueError("Routine payload must be an object")
    return value


def read_reconciliation_state(db: Session, job_name: str, session_id: int) -> dict:
    """SELECT-only expected-state evidence, without prompts, tokens or raw text."""
    jobs = _rows(
        db,
        f"SELECT * FROM {_table(db, 'claude_agent', 'routine_jobs')} WHERE name=:name",
        {"name": job_name},
    )
    agents = _rows(
        db,
        f"SELECT * FROM {_table(db, 'agent_sessions', 'agent_sessions')} WHERE id=:id",
        {"id": session_id},
    )
    if len(jobs) != 1 or len(agents) != 1:
        raise ValueError("Exact routine/session identity is missing")
    job, agent = jobs[0], agents[0]
    turns = _rows(
        db,
        f"SELECT * FROM {_table(db, 'agent_sessions', 'agent_turns')} WHERE session_id=:id ORDER BY seq LIMIT 129",
        {"id": session_id},
    )
    if not turns or len(turns) >= 129:
        raise ValueError("Turn evidence is missing or exceeds the reconciliation bound")
    pending = _rows(
        db,
        f"SELECT seq FROM {_table(db, 'agent_sessions', 'pending_messages')} WHERE session_id=:id LIMIT 1",
        {"id": session_id},
    )
    workflows = _rows(
        db,
        f"SELECT workflow_uuid,name,status,application_version FROM {_table(db, 'dbos', 'workflow_status')} WHERE workflow_uuid=:id",
        {"id": agent["workflow_id"]},
    )
    owners = _rows(
        db,
        f"SELECT id FROM {_table(db, 'agent_sessions', 'agent_sessions')} WHERE ember_session_id=:guest ORDER BY id LIMIT 2",
        {"guest": agent["ember_session_id"]},
    )
    latest_routine = _rows(
        db,
        f"SELECT id FROM {_table(db, 'agent_sessions', 'agent_sessions')} WHERE node_key=:kind AND local_session_id=workflow_id||:suffix ORDER BY id DESC LIMIT 1",
        {"kind": KG_NODE_KEY, "suffix": ":kg-drain:" + job_name},
    )
    payload = _payload(job["payload"])
    raw = None
    provenance = []
    if job_name == "kg-repo-diff":
        if payload.get("mode") != "repo-diff":
            raise ValueError("Scout payload identity mismatch")
    else:
        raw_id = payload.get("raw_id")
        if not isinstance(raw_id, str) or job_name != "kg:" + raw_id:
            raise ValueError("Raw payload identity mismatch")
        raws = _rows(
            db,
            f"SELECT id,raw_id,source,content_hash,extra FROM {_table(db, 'knowledge', 'raw_inputs')} WHERE raw_id=:id",
            {"id": raw_id},
        )
        if len(raws) != 1:
            raise ValueError("Exact raw input is missing")
        raw = raws[0]
        extra = _payload(raw["extra"] or {})
        raw["extra"] = extra
        provenance = _rows(
            db,
            f"SELECT * FROM {_table(db, 'knowledge', 'atom_raw_provenance')} WHERE raw_fk=:id AND gardener_version=:version ORDER BY id LIMIT 1001",
            {"id": raw["id"], "version": EXTRACTION_VERSION},
        )
        if len(provenance) >= 1001:
            raise ValueError("Provenance exceeds the reconciliation bound")
    latest = turns[-1]
    reservations = _rows(
        db,
        f"SELECT state,outcome,pending_seq FROM "
        f"{_table(db, 'agent_sessions', 'capacity_reservations')} "
        "WHERE session_id=:id AND pending_seq=:seq",
        {"id": session_id, "seq": latest["seq"]},
    )
    state = {
        "job_name": job_name,
        "session_id": session_id,
        "job_sha256": _sha(job),
        "payload_sha256": _sha(payload),
        "session_sha256": _sha(agent),
        "turns_sha256": _sha(turns),
        "local_session_id": agent["local_session_id"],
        "workflow_id": agent["workflow_id"],
        "workflow": workflows,
        "session_status": agent["status"],
        "node_key": agent["node_key"],
        "guest_id": agent["ember_session_id"],
        "pending": bool(pending),
        "job_status": job["last_status"],
        "routine_kind": job["routine_kind"],
        "binding_session_ids": [row["id"] for row in owners],
        "latest_routine_session_id": latest_routine[0]["id"]
        if latest_routine
        else None,
        "latest_turn_at": str(latest["created_at"]),
        "last_summary": job["last_summary"],
        "next_run_at": job["next_run_at"],
        "locked_by": job["locked_by"],
        "locked_at": job["locked_at"],
        "latest_turn_seq": latest["seq"],
        "latest_stop_reason": latest["stop_reason"],
        "latest_terminal_reason": latest["terminal_reason"],
        "reservation_state": (
            reservations[0]["state"] if len(reservations) == 1 else None
        ),
        "reservation_outcome": (
            reservations[0]["outcome"] if len(reservations) == 1 else None
        ),
        "extraction_version": EXTRACTION_VERSION,
        "raw_sha256": _sha(raw),
        "provenance_sha256": _sha(provenance),
        "provenance_count": len(provenance),
        "applied_count": sum(p["derived_note_id"] != "failed" for p in provenance),
        "extraction_passes": (raw["extra"].get("extraction_passes", 0) if raw else 0),
        "cursor_sha": payload.get("last_sha") if raw is None else None,
    }
    return {**state, "state_sha256": _sha(state)}


def _identifier(value, name, limit=256):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"Invalid {name}")
    return value


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Expected a SHA256 digest")
    return value


def _proof_time(proof):
    observed = datetime.fromisoformat(proof["observed_at"].replace("Z", "+00:00"))
    if observed.tzinfo is None:
        raise ValueError("Cessation observation must include timezone")
    return observed.astimezone(timezone.utc)


def _reconcile(db, request):
    lock_capacity_pool(db)
    request_hash = _sha(request)
    previous = db.get(RoutineReconciliation, request["reconciliation_key"])
    if previous is not None:
        if previous.request_sha256 != request_hash:
            raise ValueError("Reconciliation key conflicts with recorded evidence")
        return json.loads(previous.result_json)
    proof = request["cessation"]
    observed = _proof_time(proof)
    now = datetime.now(timezone.utc)
    if not 0 <= (now - observed).total_seconds() <= MAX_EVIDENCE_AGE_SECONDS:
        raise ValueError("Fresh cessation evidence is required")
    # Pool -> session -> job is the admission owner's lock ordering.
    agent = lock_cessation_session(db, request["session_id"])
    jobs = _table(db, "claude_agent", "routine_jobs")
    lock = " FOR UPDATE" if db.bind.dialect.name != "sqlite" else ""
    db.execute(
        text(f"SELECT name FROM {jobs} WHERE name=:name{lock}"),
        {"name": request["job_name"]},
    ).one()
    state = read_reconciliation_state(db, request["job_name"], agent["id"])
    if state["state_sha256"] != request["expected_state_sha256"]:
        raise ValueError("Expected reconciliation state changed")
    delivery_error_hold = (
        state["latest_stop_reason"] is None
        and state["latest_terminal_reason"] == "error"
        and state["reservation_state"] == "uncertain"
        and state["reservation_outcome"] == "delivery_error"
    )
    unknown_hold = (
        state["latest_stop_reason"] == UNKNOWN_INVOCATION
        and state["session_status"] == "failed"
    )
    if (
        state["job_status"] != UNKNOWN_INVOCATION
        or state["routine_kind"] != "kg-drain"
        or state["binding_session_ids"] != [agent["id"]]
        or state["latest_routine_session_id"] != agent["id"]
        or state["next_run_at"] is not None
        or state["locked_by"] is not None
        or state["locked_at"] is not None
        or not (
            unknown_hold or (delivery_error_hold and state["session_status"] == "warn")
        )
        or state["node_key"] != KG_NODE_KEY
        or state["pending"]
        or state["latest_terminal_reason"] != "error"
        or not (state["last_summary"] or "").startswith(f"session_id={agent['id']}:")
        or agent["local_session_id"]
        != f"{agent['workflow_id']}:kg-drain:{request['job_name']}"
        or len(state["workflow"]) != 1
        or state["workflow"][0]["name"] != "drain_cycle"
        or state["workflow"][0]["status"] not in {"SUCCESS", "ERROR", "CANCELLED"}
        or proof["session_id"] != state["guest_id"]
    ):
        raise ValueError("Held attempt ownership is not quiescent")
    turn_at = datetime.fromisoformat(state["latest_turn_at"].replace("Z", "+00:00"))
    if turn_at.tzinfo is None:
        turn_at = turn_at.replace(tzinfo=timezone.utc)
    if proof["updated_at"] < int(turn_at.timestamp() * 1000):
        raise ValueError("Guest cessation must follow the unknown turn")
    if db.exec(
        select(RoutineReconciliation).where(
            RoutineReconciliation.session_id == agent["id"],
            RoutineReconciliation.unknown_turn_seq == state["latest_turn_seq"],
        )
    ).first():
        raise ValueError("This held attempt was already reconciled")
    # Lock the raw against the existing atomic extraction writer before checking
    # its evidence again. The operator never changes raw/provenance records.
    if request["job_name"] != "kg-repo-diff":
        raw_table = _table(db, "knowledge", "raw_inputs")
        db.execute(
            text(f"SELECT id FROM {raw_table} WHERE raw_id=:raw{lock}"),
            {"raw": request["job_name"][3:]},
        ).one()
        if read_reconciliation_state(db, request["job_name"], agent["id"]) != state:
            raise ValueError("Provenance changed during reconciliation")
    if request["disposition"] == "rearm":
        if state["provenance_count"] or state["extraction_passes"] != 0:
            raise ValueError("Rearm requires an unprocessed raw or unchanged scout")
    elif not state["applied_count"]:
        raise ValueError("Retain-applied requires existing extraction provenance")
    if (
        not 0
        <= (datetime.now(timezone.utc) - observed).total_seconds()
        <= MAX_EVIDENCE_AGE_SECONDS
    ):
        raise ValueError("Cessation evidence expired while acquiring ownership")
    confirm_reconciled_guest_cessation(db, agent["id"], request["job_name"])
    next_run = now if request["disposition"] == "rearm" else None
    result = {
        "reconciliation_key": request["reconciliation_key"],
        "job_name": request["job_name"],
        "session_id": agent["id"],
        "unknown_turn_seq": state["latest_turn_seq"],
        "disposition": request["disposition"],
        "next_run_at": next_run.isoformat() if next_run else None,
        "original_outcome": (
            "delivery_error" if delivery_error_hold else UNKNOWN_INVOCATION
        ),
        "guest_cessation_confirmed": True,
    }
    db.execute(
        text(
            f"UPDATE {jobs} SET last_status=:status,next_run_at=:next WHERE name=:name"
        ),
        {
            "status": "reconciled_unknown",
            "next": next_run,
            "name": request["job_name"],
        },
    )
    db.flush()
    after = read_reconciliation_state(db, request["job_name"], agent["id"])
    for field in (
        "turns_sha256",
        "payload_sha256",
        "raw_sha256",
        "provenance_sha256",
        "cursor_sha",
    ):
        if after[field] != state[field]:
            raise ValueError("Reconciliation changed protected history")
    if (
        after["guest_id"] is not None
        or after["pending"]
        or after["session_status"] not in {"failed", "warn"}
    ):
        raise ValueError("Reconciliation postconditions failed")
    db.add(
        RoutineReconciliation(
            reconciliation_key=request["reconciliation_key"],
            request_sha256=request_hash,
            actor=request["actor"],
            job_name=request["job_name"],
            session_id=agent["id"],
            unknown_turn_seq=state["latest_turn_seq"],
            disposition=request["disposition"],
            evidence_json=_json({"request": request, "before": state, "after": after}),
            result_json=_json(result),
            created_at=now,
        )
    )
    db.flush()
    return result


def reconcile_held_job(
    *,
    reconciliation_key: str,
    actor: str,
    job_name: str,
    session_id: int,
    expected_state_sha256: str,
    cessation: dict,
    disposition: str,
    session: Session | None = None,
) -> dict:
    """Apply one operator-approved disposition; caller-supplied sessions own commit.

    cessation is a fresh trusted GET attestation with the exact guest identity,
    state, generation, observation and CP timestamps, and archived response hash.
    It is not accepted from guest/model output. Identical lost-response replays
    return the durable result, even after the ordinary job has run or disappeared.
    """
    if (
        type(session_id) is not int
        or session_id <= 0
        or disposition not in {"rearm", "retain_applied"}
    ):
        raise ValueError("Invalid reconciliation identity or disposition")
    required = {
        "session_id",
        "state",
        "generation",
        "observed_at",
        "updated_at",
        "last_invoke_at",
        "evidence_sha256",
    }
    if not isinstance(cessation, dict) or set(cessation) != required:
        raise ValueError("Exact cessation evidence fields are required")
    if (
        cessation["state"] not in {"parked", "evicted"}
        or type(cessation["generation"]) is not int
        or cessation["generation"] < 0
    ):
        raise ValueError("Cessation requires a parked or evicted guest")
    observed = _proof_time(cessation)
    if any(
        type(cessation[k]) is not int or cessation[k] < 0
        for k in ("updated_at", "last_invoke_at")
    ) or not cessation["last_invoke_at"] <= cessation["updated_at"] <= int(
        observed.timestamp() * 1000
    ):
        raise ValueError("Cessation timestamps conflict")
    _identifier(cessation["session_id"], "guest identity")
    _digest(cessation["evidence_sha256"])
    request = dict(
        reconciliation_key=_identifier(reconciliation_key, "key"),
        actor=_identifier(actor, "actor"),
        job_name=_identifier(job_name, "job name"),
        session_id=session_id,
        expected_state_sha256=_digest(expected_state_sha256),
        cessation=dict(cessation),
        disposition=disposition,
    )
    if session is not None:
        # Start the outer write transaction before SAVEPOINT, including SQLite's
        # deferred-BEGIN driver, so releasing a savepoint cannot commit the call.
        lock_capacity_pool(session)
        with session.begin_nested():
            return _reconcile(session, request)
    with Session(get_engine()) as db, db.begin():
        return _reconcile(db, request)
