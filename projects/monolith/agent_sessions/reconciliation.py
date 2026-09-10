"""Session-owned storage operations for explicit routine reconciliation.

The routine owner verifies exact fresh cessation evidence and holds the pool,
session and job locks. These operations never commit or infer cessation.
"""

import json
import re

from sqlmodel import Session, select

from agent_sessions import admission
from agent_sessions.models import AgentCapacityReservation, AgentSession


_CLEANUP_FIELDS = (
    "guest_cleanup_id",
    "guest_cleanup_guest_id",
    "guest_cleanup_workflow_id",
    "guest_cleanup_dispatch_json",
    "guest_cleanup_started_at",
)


def _matching_cleanup_claim(agent: AgentSession) -> dict | None:
    """Validate existing cleanup ownership, never treat its intent as proof."""
    claim = {field: getattr(agent, field) for field in _CLEANUP_FIELDS}
    if all(value is None for value in claim.values()):
        return None
    if (
        any(value is None for value in claim.values())
        or not re.fullmatch(r"[0-9a-f]{32}", agent.guest_cleanup_id or "")
        or not agent.ember_session_id
        or agent.guest_cleanup_guest_id != agent.ember_session_id
        or not agent.workflow_id
        or agent.guest_cleanup_workflow_id != agent.workflow_id
    ):
        raise ValueError("cleanup claim ownership conflict")
    try:
        dispatches = json.loads(agent.guest_cleanup_dispatch_json)
    except (TypeError, ValueError) as exc:
        raise ValueError("cleanup claim ownership conflict") from exc
    if not isinstance(dispatches, list) or any(
        not isinstance(item, dict)
        or set(item) != {"session_id", "seq", "dispatch_count", "claim_owner"}
        or item["session_id"] != agent.id
        for item in dispatches
    ):
        raise ValueError("cleanup claim ownership conflict")
    return claim


def _retire_cleanup_claim(agent: AgentSession) -> None:
    # The domain caller already validated positive exact cessation. Retiring an
    # intent alongside the binding must not change UNKNOWN history or accounting.
    _matching_cleanup_claim(agent)
    for field in _CLEANUP_FIELDS:
        setattr(agent, field, None)


def _locked_session(db: Session, session_id: int) -> AgentSession:
    return db.exec(
        select(AgentSession)
        .where(AgentSession.id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()


def lock_cessation_session(db: Session, session_id: int) -> dict:
    """After the caller's pool lock, lock and project only the required identity."""
    agent = _locked_session(db, session_id)
    return {
        "id": agent.id,
        "local_session_id": agent.local_session_id,
        "workflow_id": agent.workflow_id,
    }


def confirm_reconciled_guest_cessation(
    db: Session, session_id: int, routine_job_name: str
) -> None:
    """Settle and unbind the caller's verified ceased attempt in its transaction.

    The routine owner must already hold pool/session/job ownership and validate
    its fresh authoritative cessation proof. Claimed or newer execution remains
    fenced. The caller owns the audit and commits all changes together.
    """
    agent = _locked_session(db, session_id)
    _matching_cleanup_claim(agent)
    admission.adopt_existing(db)
    if db.exec(
        select(AgentCapacityReservation).where(
            AgentCapacityReservation.routine_job_name == routine_job_name,
            AgentCapacityReservation.state != "settled",
            (
                (AgentCapacityReservation.session_id != agent.id)
                | AgentCapacityReservation.session_id.is_(None)
                | AgentCapacityReservation.state.in_(("reserved", "running"))
            ),
        )
    ).first():
        raise ValueError("A newer or still admitted execution owns this job")
    admission.confirm_guest_cessation(db, agent)
    if db.exec(
        select(AgentCapacityReservation).where(
            AgentCapacityReservation.session_id == agent.id,
            AgentCapacityReservation.state != "settled",
        )
    ).first():
        raise ValueError("Attempt still owns an execution permit")
    if agent.ember_lineage_id:
        agent.prior_ember_lineage_id = agent.ember_lineage_id
    if agent.cli_session_id:
        agent.prior_cli_session_id = agent.cli_session_id
    _retire_cleanup_claim(agent)
    agent.ember_session_id = None
    agent.ember_session_token = None
    agent.ember_session_expires_at = None
    agent.ember_lineage_id = None
    agent.cli_session_id = None
    db.add(agent)


def _factory_owner(db: Session, pin: dict, session_id: int | None):
    from agent_sessions import normalize_model

    key = f"factory:{pin['task_id']}:{pin['node_key']}:{pin['attempt']}"
    owner = db.exec(
        select(AgentSession)
        .where(AgentSession.local_session_id == key)
        .execution_options(populate_existing=True)
    ).first()
    if owner is None:
        return None
    expected = {
        "workflow_id": pin["workflow_id"],
        "node_key": pin["node_key"],
        "node_attempt": pin["attempt"],
        "repo": pin["repo"],
        "branch": pin.get("hydration_branch", pin["branch"]),
        "model": normalize_model(pin["model"]),
        "admission_tier": "project",
    }
    if (session_id is not None and owner.id != session_id) or any(
        getattr(owner, field) != value for field, value in expected.items()
    ):
        raise ValueError("factory session ownership conflict")
    return owner


def read_not_invoked_factory_attempt(
    db: Session, pin: dict, session_id: int | None
) -> dict | None:
    """Validate the session owner's completed first-dispatch failure.

    The factory caller holds its control lock and keeps this transaction open
    through graph/start settlement. This reads positive, paired turn/permit
    evidence under pool/session locks; it never settles capacity, clears a
    binding, or infers physical cessation from an absent guest.
    """
    from datetime import datetime, timezone
    from sqlalchemy import or_

    from agent_sessions import normalize_model
    from agent_sessions.models import AgentResultReceipt, AgentTurn, PendingMessage

    if session_id is not None and (type(session_id) is not int or session_id < 1):
        raise ValueError("invalid factory session identity")
    admission.lock_pool(db)
    agent = _factory_owner(db, pin, session_id)
    if agent is None:
        return None
    agent = _locked_session(db, agent.id)
    if _factory_owner(db, pin, agent.id) is None:
        return None
    if (
        agent.status != "warn"
        or agent.result_receipt_fence_id is not None
        or admission.cleanup_pending(db, agent)
        or db.exec(
            select(PendingMessage.id).where(PendingMessage.session_id == agent.id)
        ).first()
        is not None
    ):
        return None
    turns = db.exec(
        select(AgentTurn).where(AgentTurn.session_id == agent.id).limit(2)
    ).all()
    if len(turns) != 1:
        return None
    turn = turns[0]
    if (
        turn.seq != 1
        or turn.terminal_reason != "error"
        or turn.stop_reason is not None
        or turn.cost_usd is not None
        or turn.model != normalize_model(pin["model"])
    ):
        return None
    try:
        usage = json.loads(turn.usage_json or "{}")
        recovery = usage.get("recovery", {})
        if (
            not isinstance(recovery, dict)
            or recovery.get("invocation_phase") != "not_invoked"
            or type(recovery.get("dispatch_count")) is not int
            or recovery["dispatch_count"] != 1
            or not isinstance(recovery.get("claim_owner"), str)
            or not recovery["claim_owner"]
            or not isinstance(recovery.get("last_dispatch_at"), str)
        ):
            return None
        dispatched = datetime.fromisoformat(
            recovery["last_dispatch_at"].replace("Z", "+00:00")
        )
    except (AttributeError, TypeError, ValueError):
        return None

    def aware(value):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

    permits = db.exec(
        select(AgentCapacityReservation)
        .where(
            or_(
                AgentCapacityReservation.session_id == agent.id,
                AgentCapacityReservation.local_session_id == agent.local_session_id,
            )
        )
        .execution_options(populate_existing=True)
        .limit(2)
    ).all()
    if len(permits) != 1:
        return None
    permit = permits[0]
    if (
        permit.session_id != agent.id
        or permit.local_session_id != agent.local_session_id
        or permit.pending_seq != 1
        or permit.tier != "project"
        or permit.routine_job_name is not None
        or permit.model != normalize_model(pin["model"])
        or permit.state != "settled"
        or permit.outcome != "not_invoked"
        or permit.owner != recovery["claim_owner"]
        or permit.settled_at is None
        or not aware(dispatched) <= aware(turn.created_at) <= aware(permit.settled_at)
    ):
        return None
    # A prepared empty receipt can precede a failed POST setup. Any captured
    # response, response marker, different owner or newer attempt conflicts
    # with this proof. Read metadata only, never a native result body.
    receipts = db.exec(
        select(
            AgentResultReceipt.local_session_id,
            AgentResultReceipt.session_id,
            AgentResultReceipt.seq,
            AgentResultReceipt.dispatch_count,
            AgentResultReceipt.claim_owner,
            AgentResultReceipt.guest_id,
            AgentResultReceipt.received_at,
            AgentResultReceipt.response_observed_at,
            AgentResultReceipt.result_sha256,
            AgentResultReceipt.result_body.isnot(None),
        )
        .where(
            or_(
                AgentResultReceipt.session_id == agent.id,
                AgentResultReceipt.local_session_id == agent.local_session_id,
            )
        )
        .with_for_update()
        .limit(2)
    ).all()
    if len(receipts) > 1 or any(
        tuple(receipt[:6])
        != (
            agent.local_session_id,
            agent.id,
            1,
            1,
            permit.owner,
            agent.ember_session_id,
        )
        or any(value is not None for value in receipt[6:9])
        or receipt[9]
        for receipt in receipts
    ):
        return None
    return {
        "session_id": agent.id,
        "local_session_id": agent.local_session_id,
        "workflow_id": agent.workflow_id,
        "turn_id": turn.id,
        "seq": 1,
        "dispatch_count": 1,
        "claim_owner": permit.owner,
        "last_dispatch_at": recovery["last_dispatch_at"],
        "permit_id": permit.id,
        "permit_outcome": permit.outcome,
        "invocation_phase": "not_invoked",
        "cost_usd": None,
    }


def read_uncertain_factory_attempt(db: Session, pin: dict, session_id: int) -> dict:
    """Lock and fingerprint the exact failed executor, never its replacement.

    The factory caller holds its control lock. This function acquires the pool
    before the session, and returns no prompt, result body, or credential.
    """
    import hashlib
    import json
    from datetime import datetime, timezone
    from sqlalchemy import or_

    from agent_sessions.constants import UNKNOWN_INVOCATION
    from agent_sessions.models import AgentTurn, PendingMessage

    admission.lock_pool(db)
    agent = _factory_owner(db, pin, session_id)
    if agent is None:
        raise ValueError("missing_factory_owner")
    agent = _locked_session(db, agent.id)
    if _factory_owner(db, pin, session_id) is None:
        raise ValueError("factory_owner_changed")
    _matching_cleanup_claim(agent)
    if (
        not agent.ember_session_id
        or agent.result_receipt_fence_id is not None
        or db.exec(
            select(PendingMessage.id).where(PendingMessage.session_id == agent.id)
        ).first()
        is not None
    ):
        raise ValueError("pending_or_missing_factory_guest")
    turns = db.exec(
        select(AgentTurn).where(AgentTurn.session_id == agent.id).limit(2)
    ).all()
    permits = db.exec(
        select(AgentCapacityReservation)
        .where(
            or_(
                AgentCapacityReservation.session_id == agent.id,
                AgentCapacityReservation.local_session_id == agent.local_session_id,
            )
        )
        .execution_options(populate_existing=True)
        .limit(2)
    ).all()
    owners = db.exec(
        select(AgentSession.id)
        .where(AgentSession.ember_session_id == agent.ember_session_id)
        .limit(2)
    ).all()
    if len(turns) != 1 or len(permits) != 1 or owners != [agent.id]:
        raise ValueError("ambiguous_factory_attempt")
    turn, permit = turns[0], permits[0]
    if (
        turn.seq != 1
        or turn.terminal_reason != "error"
        or permit.pending_seq != 1
        or permit.session_id != agent.id
        or permit.local_session_id != agent.local_session_id
        or permit.state != "uncertain"
        or permit.tier != "project"
        or permit.routine_job_name is not None
        or not permit.owner
        or not (
            (agent.status == "failed" and turn.stop_reason == UNKNOWN_INVOCATION)
            or (
                agent.status == "warn"
                and turn.stop_reason is None
                and permit.outcome == "delivery_error"
            )
        )
    ):
        raise ValueError("factory_attempt_not_uncertain")
    recovery = json.loads(turn.usage_json or "{}").get("recovery", {})
    count = recovery.get("dispatch_count")
    if (
        type(count) is not int
        or count < 1
        or recovery.get("claim_owner") != permit.owner
        or not isinstance(recovery.get("last_dispatch_at"), str)
    ):
        raise ValueError("missing_factory_dispatch_identity")
    dispatched = datetime.fromisoformat(
        recovery["last_dispatch_at"].replace("Z", "+00:00")
    )
    if dispatched.tzinfo is None:
        dispatched = dispatched.replace(tzinfo=timezone.utc)

    def encode(value):
        if isinstance(value, bytes):
            return {"sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
        if isinstance(value, datetime):
            return (
                value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
            ).isoformat()
        raise TypeError(type(value).__name__)

    failed_turn_at = (
        turn.created_at.replace(tzinfo=timezone.utc)
        if turn.created_at.tzinfo is None
        else turn.created_at
    )
    protected = {
        "pin": pin,
        "permit": permit.model_dump(),
        "turn": turn.model_dump(),
        "failed_turn_at": failed_turn_at,
        "session_id": agent.id,
        "local_session_id": agent.local_session_id,
        "guest_id": agent.ember_session_id,
        "status": agent.status,
        "last_turn_at": agent.last_turn_at,
        "lineage_id": agent.ember_lineage_id,
        "cli_session_id": agent.cli_session_id,
        "prior_lineage_id": agent.prior_ember_lineage_id,
        "prior_cli_session_id": agent.prior_cli_session_id,
    }
    fingerprint = hashlib.sha256(
        json.dumps(protected, sort_keys=True, default=encode).encode()
    ).hexdigest()
    return {
        "session_id": agent.id,
        "guest_id": agent.ember_session_id,
        "permit_id": permit.id,
        "seq": turn.seq,
        "claim_owner": permit.owner,
        "dispatch_count": count,
        "dispatched_at": dispatched.isoformat(),
        # Stamped when the failure was recorded, so it sits AFTER the invoke
        # this attempt made, where dispatched_at (the claim stamp) sits before
        # it. Cessation ordering anchors on this one.
        "failed_turn_at": failed_turn_at.isoformat(),
        "identity_sha256": fingerprint,
        "cost_usd": turn.cost_usd,
    }


def settle_uncertain_factory_attempt(db: Session, pin: dict, identity: dict) -> None:
    """Settle only after the factory validates its exact durable stop proof.

    Caller composes graph/start/audit settlement in this transaction. The
    original turn and its UNKNOWN/cost/artifacts are deliberately immutable.
    """
    current = read_uncertain_factory_attempt(db, pin, identity["session_id"])
    if current != identity:
        raise ValueError("factory_attempt_changed")
    agent = _locked_session(db, identity["session_id"])
    admission.settle(
        db,
        agent,
        identity["seq"],
        outcome="guest_cessation_confirmed",
        cessation_confirmed=True,
    )
    if agent.ember_lineage_id:
        agent.prior_ember_lineage_id = agent.ember_lineage_id
    if agent.cli_session_id:
        agent.prior_cli_session_id = agent.cli_session_id
    _retire_cleanup_claim(agent)
    agent.ember_session_id = None
    agent.ember_session_token = None
    agent.ember_session_expires_at = None
    agent.ember_lineage_id = None
    agent.cli_session_id = None
    db.add(agent)


def read_factory_dispatch(db: Session, pin: dict, session_id: int) -> dict:
    """Project persisted claim evidence without treating a missing row as queued."""
    from agent_sessions.models import PendingMessage

    owner = _factory_owner(db, pin, session_id)
    pending = (
        db.exec(
            select(PendingMessage).where(PendingMessage.session_id == session_id)
        ).all()
        if owner is not None
        else []
    )
    unknown = {"state": "unconfirmed", "started_at": None}
    if len(pending) != 1 or pending[0].seq != 1:
        return unknown
    row = pending[0]
    if row.last_dispatch_at is not None and row.dispatch_count == 1:
        return {"state": "dispatched", "started_at": row.last_dispatch_at.isoformat()}
    if (
        row.dispatch_count
        or row.last_dispatch_at is not None
        or row.claimed_by_replica is not None
        or row.claimed_at is not None
        or row.partial_text
        or row.partial_activities
    ):
        return unknown
    return {"state": "queued", "started_at": None}


def cancel_queued_factory_attempt(db: Session, pin: dict, session_id: int | None):
    """Terminate an exact never-dispatched first turn, without committing.

    The factory caller holds its control lock and composes graph/start outcomes
    in this transaction. The pool lock serializes against dispatch before any
    observations. Null guest bindings alone are never proof of no invocation.
    """
    import json
    from datetime import datetime, timezone
    from sqlalchemy import or_
    from agent_sessions.models import AgentTurn, PendingMessage

    admission.lock_pool(db)
    owner = _factory_owner(db, pin, session_id)
    if owner is None:
        return None
    owner = _locked_session(db, owner.id)
    pending = db.exec(
        select(PendingMessage)
        .where(PendingMessage.session_id == owner.id)
        .execution_options(populate_existing=True)
    ).all()
    if len(pending) != 1 or pending[0].seq != 1:
        return None
    row = pending[0]
    if (
        db.exec(select(AgentTurn.id).where(AgentTurn.session_id == owner.id)).first()
        is not None
    ):
        return None
    if any(
        (
            owner.ember_session_id,
            owner.ember_session_token,
            owner.ember_lineage_id,
            owner.cli_session_id,
            owner.prior_ember_lineage_id,
            owner.prior_cli_session_id,
            owner.ember_session_expires_at,
            owner.recovery_workspace_loss,
        )
    ):
        return None
    if owner.status not in {"running", "warn"}:
        return None
    if (
        row.dispatch_count != 0
        or row.claimed_by_replica is not None
        or row.claimed_at is not None
        or row.last_dispatch_at is not None
        or row.partial_text
        or row.partial_activities
    ):
        return None

    def aware(value):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

    # Initial creation precedes enqueue. A later last_turn_at is valid only
    # when the existing one-shot recovery wrote its matching completion marker;
    # that path never resets dispatch_count and refuses attempted messages.
    if aware(owner.last_turn_at) > aware(row.created_at) and (
        owner.recovery_completed_at is None
        or aware(owner.last_turn_at) != aware(owner.recovery_completed_at)
    ):
        return None
    permits = db.exec(
        select(AgentCapacityReservation)
        .where(
            or_(
                AgentCapacityReservation.local_session_id == owner.local_session_id,
                AgentCapacityReservation.session_id == owner.id,
            )
        )
        .execution_options(populate_existing=True)
    ).all()
    if len(permits) > 1 or any(
        permit.local_session_id != owner.local_session_id
        or permit.session_id not in (None, owner.id)
        or permit.pending_seq != 1
        or permit.state != "reserved"
        or permit.owner is not None
        or permit.workload is not None
        or permit.outcome is not None
        or permit.settled_at is not None
        or permit.tier != "project"
        for permit in permits
    ):
        return None
    if not admission.cancel_unattempted(db, owner, row):
        return None
    db.add(
        AgentTurn(
            session_id=owner.id,
            seq=1,
            prompt=row.message_text,
            model=None,
            cost_usd=None,
            terminal_reason="error",
            stop_reason="cancelled_before_dispatch",
            result_text="Factory reconciliation cancelled this queued attempt after its workflow timed out. No agent invocation was dispatched.",
            usage_json=json.dumps(
                {
                    "source": "factory_reconciliation",
                    "reason": "cancelled_before_dispatch",
                }
            ),
        )
    )
    owner.status = "warn"
    owner.last_turn_at = datetime.now(timezone.utc)
    db.add(owner)
    db.delete(row)
    db.flush()
    return owner.id
