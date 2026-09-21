"""Session-owned storage operations for explicit routine reconciliation.

The routine owner verifies exact fresh cessation evidence and holds the pool,
session and job locks. These operations never commit or infer cessation.
"""

import json
import re
from datetime import datetime, timezone

from sqlmodel import Session, select

from factory.execution import admission
from factory.execution.constants import UNKNOWN_INVOCATION
from factory.execution.models import AgentCapacityReservation, AgentSession
from factory.utils import sanitize_payload


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


def confirm_reconciled_unbound_attempt(
    db: Session,
    session_id: int,
    routine_job_name: str,
    permit_id: int,
    identity_sha256: str,
) -> None:
    """Revalidate no guest delivery and settle inside the routine transaction.

    This is not a claim that a remote VM was destroyed. The exact failed claim
    has no binding or prior-binding evidence, pending executor, or model output.
    The unknown turn remains immutable and prevents a late executor from binding
    a guest or sending another turn. An idle VM allocated before response loss
    remains the control plane's lifecycle responsibility.
    """
    from factory.execution.permit_supervision import _identity, _NO_GUEST_OUTCOMES

    permit = db.exec(
        select(AgentCapacityReservation)
        .where(AgentCapacityReservation.id == permit_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if (
        permit is None
        or permit.session_id != session_id
        or permit.routine_job_name != routine_job_name
        or permit.outcome not in _NO_GUEST_OUTCOMES
    ):
        raise ValueError("Unbound routine permit changed")
    agent, turn, identity = _identity(db, permit)
    if (
        agent.ember_session_id is not None
        or identity != identity_sha256
        or turn.stop_reason != UNKNOWN_INVOCATION
    ):
        raise ValueError("Unbound routine proof changed")
    if db.exec(
        select(AgentCapacityReservation).where(
            AgentCapacityReservation.routine_job_name == routine_job_name,
            AgentCapacityReservation.state != "settled",
            AgentCapacityReservation.id != permit_id,
        )
    ).first():
        raise ValueError("Another execution owns this job")
    admission.settle(
        db,
        agent,
        permit.pending_seq,
        outcome="no_guest_bound",
        cessation_confirmed=True,
    )


def _factory_owner(db: Session, pin: dict, session_id: int | None):
    from factory.execution import normalize_model

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


_NEVER_DISPATCHED_WORKFLOW_STATUSES = frozenset({"CANCELLED", "ERROR"})


def read_never_dispatched_factory_attempt(
    db: Session,
    pin: dict,
    session_id: int | None,
    workflow_status: str | None,
) -> dict | None:
    """Prove an exact factory attempt was stranded before its first dispatch.

    The terminal workflow is external evidence that no executor can still own
    this queued message. The factory caller holds its control lock and keeps
    this transaction open through session, graph and start settlement. The
    pool/session/message locks serialize this read against a first claim.

    A missing guest is only one required observation. Any turn, claim,
    dispatch, receipt, binding history, or non-reserved permit contradicts the
    proof. This shape is deliberately separate from
    read_not_invoked_factory_attempt, whose first dispatch was claimed and
    whose dispatch_count is exactly one.
    """
    from datetime import timezone
    from sqlalchemy import or_

    from factory.execution import normalize_model
    from factory.execution.models import (
        AgentResultReceipt,
        AgentTurn,
        PendingMessage,
    )

    if workflow_status not in _NEVER_DISPATCHED_WORKFLOW_STATUSES:
        return None
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
        agent.status != "running"
        or agent.result_receipt_fence_id is not None
        or admission.cleanup_pending(db, agent)
        or agent.recovery_completed_at is not None
    ):
        return None
    if any(
        getattr(agent, field) is not None
        for field in (
            "ember_session_id",
            "ember_session_token",
            "ember_session_expires_at",
            "ember_lineage_id",
            "prior_ember_lineage_id",
            "cli_session_id",
            "prior_cli_session_id",
            "guest_cleanup_id",
            "guest_cleanup_guest_id",
            "guest_cleanup_workflow_id",
            "guest_cleanup_dispatch_json",
            "guest_cleanup_started_at",
            "recovery_workspace_loss",
        )
    ):
        return None
    if (
        db.exec(
            select(AgentTurn.id)
            .where(AgentTurn.session_id == agent.id)
            .with_for_update()
        ).first()
        is not None
    ):
        return None
    pending = db.exec(
        select(PendingMessage)
        .where(PendingMessage.session_id == agent.id)
        .with_for_update()
        .execution_options(populate_existing=True)
        .limit(2)
    ).all()
    if len(pending) != 1:
        return None
    message = pending[0]
    if (
        message.seq != 1
        or message.model != normalize_model(pin["model"])
        or type(message.dispatch_count) is not int
        or message.dispatch_count != 0
        or message.claimed_by_replica is not None
        or message.claimed_at is not None
        or message.last_dispatch_at is not None
        or message.partial_text is not None
        or message.partial_activities is not None
    ):
        return None

    def aware(value):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

    if aware(agent.last_turn_at) > aware(message.created_at):
        return None
    permits = db.exec(
        select(AgentCapacityReservation)
        .where(
            or_(
                AgentCapacityReservation.local_session_id == agent.local_session_id,
                AgentCapacityReservation.session_id == agent.id,
            )
        )
        .with_for_update()
        .execution_options(populate_existing=True)
        .limit(2)
    ).all()
    if len(permits) > 1 or any(
        permit.local_session_id != agent.local_session_id
        or permit.session_id not in (None, agent.id)
        or permit.pending_seq != 1
        or permit.tier != "project"
        or permit.model != normalize_model(pin["model"])
        or permit.daily_key is not None
        or permit.routine_job_name is not None
        or permit.state != "reserved"
        or permit.owner is not None
        or permit.workload is not None
        or permit.outcome is not None
        or permit.settled_at is not None
        for permit in permits
    ):
        return None
    if (
        db.exec(
            select(AgentResultReceipt.id)
            .where(
                or_(
                    AgentResultReceipt.session_id == agent.id,
                    AgentResultReceipt.local_session_id == agent.local_session_id,
                )
            )
            .with_for_update()
        ).first()
        is not None
    ):
        return None
    permit = permits[0] if permits else None
    return {
        "session_id": agent.id,
        "local_session_id": agent.local_session_id,
        "workflow_id": agent.workflow_id,
        "workflow_status": workflow_status,
        "message_id": message.id,
        "seq": message.seq,
        "dispatch_count": 0,
        "permit_id": None if permit is None else permit.id,
        "invocation_phase": "never_dispatched",
        "cost_usd": 0.0,
    }


def settle_never_dispatched_factory_attempt(
    db: Session, pin: dict, proof: dict
) -> None:
    """Fail and consume a factory message proven never to have dispatched."""
    from factory.execution.models import PendingMessage

    current = read_never_dispatched_factory_attempt(
        db,
        pin,
        proof["session_id"],
        proof["workflow_status"],
    )
    if current != proof:
        raise ValueError("factory_attempt_changed")
    agent = _locked_session(db, proof["session_id"])
    message = db.exec(
        select(PendingMessage)
        .where(PendingMessage.id == proof["message_id"])
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if not admission.cancel_unattempted(db, agent, message):
        raise ValueError("factory_attempt_changed")
    permit = admission.reservation(db, agent.local_session_id, message.seq)
    if permit is not None:
        permit.outcome = "never_dispatched"
        db.add(permit)
    agent.status = "failed"
    db.add(agent)
    db.delete(message)
    db.flush()


# EmberVM refuses a create it cannot place with 429 and a machine readable
# reason in the body: see create_denial/3 in the control plane router, whose
# 429 set is exactly these four. The body survives into the turn, because
# transport._status_error_detail appends it to the status line and the
# not-invoked path persists that whole string, so the reason is what is matched
# here rather than the bare status. The endpoint is matched too: a create the
# control plane would not place is the refusal that did no work at all.
_CAPACITY_DENIAL_REASON = re.compile(
    r'"reason"\s*:\s*"(?:session_cap|workload_cap|quota|no_capacity)"'
)
_CAPACITY_DENIAL_STATUS = "429 Too Many Requests"
_CAPACITY_DENIAL_ENDPOINT = "/sessions"


def _capacity_denied_turn(turn) -> bool:
    """True when this failed turn's error is the control plane refusing a slot.

    Read together with the not-invoked evidence, never alone: the phase proves
    the attempt never reached its model, and this says the reason was that
    EmberVM had no slot for it rather than anything about the attempt itself.
    A turn whose error text lost the body, to truncation or to a partial
    result, reads as an ordinary failure and spends its attempt.
    """
    text = f"{turn.voice_summary or ''}\n{turn.result_text or ''}"
    return (
        _CAPACITY_DENIAL_STATUS in text
        and _CAPACITY_DENIAL_ENDPOINT in text
        and _CAPACITY_DENIAL_REASON.search(text) is not None
    )


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

    from factory.execution import normalize_model
    from factory.execution.models import AgentResultReceipt, AgentTurn, PendingMessage

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
        # Whether the control plane refused the slot, which the factory reads
        # to decide if this failure spends one of the node's attempts (#6045).
        "capacity_denied": _capacity_denied_turn(turn),
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

    from factory.execution.constants import UNKNOWN_INVOCATION
    from factory.execution.models import AgentTurn, PendingMessage

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


def read_bound_zero_turn_factory_attempt(
    db: Session, pin: dict, session_id: int
) -> dict:
    """Lock the exact bound dispatch that has no local turn result.

    This is the narrow post-guest response-loss shape from #6288. A binding is
    evidence that a guest may have run, never evidence that it did not. The
    caller must separately prove unchanged control-plane completion or
    sustained authoritative absence before fencing or settling this identity.

    The cleanup fields may contain this proof's exact durable fence. Any other
    cleanup owner, receipt result, turn, message mutation, replacement owner,
    or permit mutation refuses the proof.
    """
    import hashlib
    from datetime import datetime, timezone

    from sqlalchemy import or_

    from factory.execution.models import (
        AgentResultReceipt,
        AgentTurn,
        PendingMessage,
    )

    admission.lock_pool(db)
    agent = _factory_owner(db, pin, session_id)
    if agent is None:
        raise ValueError("missing_factory_owner")
    agent = _locked_session(db, agent.id)
    if _factory_owner(db, pin, session_id) is None:
        raise ValueError("factory_owner_changed")
    claim = _matching_cleanup_claim(agent)
    owners = db.exec(
        select(AgentSession.id)
        .where(AgentSession.ember_session_id == agent.ember_session_id)
        .limit(2)
    ).all()
    turns = db.exec(
        select(AgentTurn.id)
        .where(AgentTurn.session_id == agent.id)
        .with_for_update()
        .limit(1)
    ).all()
    pending = db.exec(
        select(PendingMessage)
        .where(PendingMessage.session_id == agent.id)
        .with_for_update()
        .execution_options(populate_existing=True)
        .limit(2)
    ).all()
    permits = db.exec(
        select(AgentCapacityReservation)
        .where(
            or_(
                AgentCapacityReservation.session_id == agent.id,
                AgentCapacityReservation.local_session_id == agent.local_session_id,
            )
        )
        .with_for_update()
        .execution_options(populate_existing=True)
        .limit(2)
    ).all()
    receipts = db.exec(
        select(AgentResultReceipt)
        .where(
            or_(
                AgentResultReceipt.session_id == agent.id,
                AgentResultReceipt.local_session_id == agent.local_session_id,
            )
        )
        .with_for_update()
        .execution_options(populate_existing=True)
        .limit(2)
    ).all()
    if (
        agent.status != "running"
        or not agent.ember_session_id
        or agent.result_receipt_fence_id is not None
        or agent.recovery_completed_at is not None
        or owners != [agent.id]
        or turns
        or len(pending) != 1
        or len(permits) != 1
        or len(receipts) > 1
    ):
        raise ValueError("factory_bound_zero_turn_not_applicable")
    message, permit = pending[0], permits[0]
    if (
        message.seq != 1
        or message.model != permit.model
        or message.claimed_by_replica is None
        or message.claimed_by_replica != permit.owner
        or not isinstance(message.claimed_at, datetime)
        or type(message.dispatch_count) is not int
        or message.dispatch_count < 1
        or not isinstance(message.last_dispatch_at, datetime)
        or message.partial_text is not None
        or message.partial_activities is not None
        or permit.pending_seq != message.seq
        or permit.session_id != agent.id
        or permit.local_session_id != agent.local_session_id
        or permit.state not in {"running", "uncertain"}
        or permit.tier != "project"
        or permit.routine_job_name is not None
        or not permit.owner
        or permit.settled_at is not None
        or permit.outcome not in {None, "delivery_error"}
    ):
        raise ValueError("factory_bound_zero_turn_identity_changed")
    receipt_id = None
    if receipts:
        receipt = receipts[0]
        if (
            receipt.session_id != agent.id
            or receipt.local_session_id != agent.local_session_id
            or receipt.seq != message.seq
            or receipt.dispatch_count != message.dispatch_count
            or receipt.claim_owner != message.claimed_by_replica
            or receipt.guest_id != agent.ember_session_id
            or receipt.superseded_at is not None
        ):
            raise ValueError("factory_bound_zero_turn_receipt_changed")
        if receipt.result_sha256 is not None or receipt.received_at is not None:
            raise ValueError("factory_bound_zero_turn_result_committed")
        receipt_id = receipt.id

    def stamp(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()

    protected = {
        "pin": pin,
        "session_id": agent.id,
        "local_session_id": agent.local_session_id,
        "workflow_id": agent.workflow_id,
        "guest_id": agent.ember_session_id,
        "status": agent.status,
        "lineage_id": agent.ember_lineage_id,
        "cli_session_id": agent.cli_session_id,
        "pending_id": message.id,
        "seq": message.seq,
        "model": message.model,
        "dispatch_count": message.dispatch_count,
        "claim_owner": message.claimed_by_replica,
        "claimed_at": stamp(message.claimed_at),
        "dispatched_at": stamp(message.last_dispatch_at),
        "permit_id": permit.id,
        "permit_state": permit.state,
        "permit_outcome": permit.outcome,
        "receipt_id": receipt_id,
    }
    fingerprint = hashlib.sha256(
        json.dumps(protected, sort_keys=True).encode()
    ).hexdigest()
    dispatches = [
        {
            "session_id": agent.id,
            "seq": message.seq,
            "dispatch_count": message.dispatch_count,
            "claim_owner": message.claimed_by_replica,
        }
    ]
    from factory.execution.constants import BOUND_ZERO_TURN_CLEANUP_PREFIX

    cleanup_claim_id = BOUND_ZERO_TURN_CLEANUP_PREFIX + fingerprint[:24]
    expected_claim = {
        "guest_cleanup_id": cleanup_claim_id,
        "guest_cleanup_guest_id": agent.ember_session_id,
        "guest_cleanup_workflow_id": agent.workflow_id,
        "guest_cleanup_dispatch_json": json.dumps(dispatches, sort_keys=True),
    }
    if claim is not None and any(
        claim[field] != value for field, value in expected_claim.items()
    ):
        raise ValueError("factory_bound_zero_turn_cleanup_changed")
    return {
        **protected,
        "identity_sha256": fingerprint,
        "cleanup_claim_id": cleanup_claim_id,
        "cleanup_fenced": claim is not None,
        "cost_usd": None,
    }


def fence_bound_zero_turn_factory_attempt(
    db: Session, pin: dict, identity: dict
) -> None:
    """Fence the exact pending dispatch before conditional guest cleanup.

    Closing an existing receipt's acceptance window under the same receipt-row
    lock gives its callback an exact ordering against this fence. A body that
    committed first makes the identity read fail. Once this transaction wins,
    the stale callback is rejected and cannot strand the fenced settlement.
    """
    current = read_bound_zero_turn_factory_attempt(db, pin, identity["session_id"])
    if current != identity:
        raise ValueError("factory_bound_zero_turn_attempt_changed")
    if current["cleanup_fenced"]:
        return
    agent = _locked_session(db, identity["session_id"])
    dispatches = [
        {
            "session_id": identity["session_id"],
            "seq": identity["seq"],
            "dispatch_count": identity["dispatch_count"],
            "claim_owner": identity["claim_owner"],
        }
    ]
    agent.guest_cleanup_id = identity["cleanup_claim_id"]
    agent.guest_cleanup_guest_id = identity["guest_id"]
    agent.guest_cleanup_workflow_id = identity["workflow_id"]
    agent.guest_cleanup_dispatch_json = json.dumps(dispatches, sort_keys=True)
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    if identity["receipt_id"] is not None:
        from factory.execution.models import AgentResultReceipt

        receipt = db.exec(
            select(AgentResultReceipt)
            .where(AgentResultReceipt.id == identity["receipt_id"])
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        if receipt.result_sha256 is not None or receipt.received_at is not None:
            raise ValueError("factory_bound_zero_turn_result_committed")
        receipt.accept_until = now
        db.add(receipt)
    agent.guest_cleanup_started_at = now
    db.add(agent)
    db.flush()


def release_bound_zero_turn_factory_fence(
    db: Session, pin: dict, identity: dict
) -> None:
    """Release only this proof's fence when remote progress invalidates it."""
    current = read_bound_zero_turn_factory_attempt(db, pin, identity["session_id"])
    if (
        current["identity_sha256"] != identity["identity_sha256"]
        or not current["cleanup_fenced"]
    ):
        raise ValueError("factory_bound_zero_turn_attempt_changed")
    agent = _locked_session(db, identity["session_id"])
    _retire_cleanup_claim(agent)
    db.add(agent)
    db.flush()


def settle_bound_zero_turn_factory_attempt(
    db: Session, pin: dict, identity: dict
) -> None:
    """Consume one fenced post-guest loss after exact cessation proof.

    The bound guest may have spent money, so this deliberately creates no
    measured-zero turn. The caller records cost_usd=None, retaining the
    reservation ceiling, while this storage transaction consumes the exact
    pending row and settles its permit as delivery_error.
    """
    from datetime import datetime, timezone

    from factory.execution.models import PendingMessage

    current = read_bound_zero_turn_factory_attempt(db, pin, identity["session_id"])
    if current != identity or not current["cleanup_fenced"]:
        raise ValueError("factory_bound_zero_turn_attempt_changed")
    agent = _locked_session(db, identity["session_id"])
    message = db.exec(
        select(PendingMessage)
        .where(PendingMessage.id == identity["pending_id"])
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    admission.settle(
        db,
        agent,
        identity["seq"],
        outcome="delivery_error",
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
    agent.progress_token = None
    agent.status = "failed"
    agent.last_turn_at = datetime.now(timezone.utc)
    db.add(agent)
    db.delete(message)
    db.flush()


def read_drained_lost_factory_attempt(db: Session, pin: dict, session_id: int) -> dict:
    """Lock one resumable drain without treating it as an unknown invocation.

    This is deliberately disjoint from ``read_uncertain_factory_attempt``.
    A drain is a successful, durable interruption whose pending row is its
    continuation grant. It becomes eligible here only while that exact grant
    is unclaimed and still names the same turn, permit, guest and dispatch.
    Remote cessation and permanent-loss proof are validated by the factory
    supervisor before the separate settlement call consumes this identity.
    """
    import hashlib
    from datetime import datetime, timezone

    from sqlalchemy import or_

    from factory.execution import normalize_model
    from factory.execution.constants import exact_dispatch_id
    from factory.execution.models import AgentTurn, PendingMessage

    admission.lock_pool(db)
    agent = _factory_owner(db, pin, session_id)
    if agent is None:
        raise ValueError("missing_factory_owner")
    agent = _locked_session(db, agent.id)
    if _factory_owner(db, pin, session_id) is None:
        raise ValueError("factory_owner_changed")
    _matching_cleanup_claim(agent)
    if (
        agent.status != "recovering"
        or not agent.ember_session_id
        or not agent.ember_lineage_id
        or not agent.cli_session_id
        or agent.prior_ember_lineage_id is not None
        or agent.prior_cli_session_id is not None
        or agent.result_receipt_fence_id is not None
        or admission.cleanup_pending(db, agent)
        or agent.recovery_completed_at is not None
    ):
        raise ValueError("factory_drain_not_resumable")
    owners = db.exec(
        select(AgentSession.id)
        .where(AgentSession.ember_session_id == agent.ember_session_id)
        .limit(2)
    ).all()
    turns = db.exec(
        select(AgentTurn)
        .where(AgentTurn.session_id == agent.id)
        .with_for_update()
        .execution_options(populate_existing=True)
        .limit(2)
    ).all()
    pending = db.exec(
        select(PendingMessage)
        .where(PendingMessage.session_id == agent.id)
        .with_for_update()
        .execution_options(populate_existing=True)
        .limit(2)
    ).all()
    permits = db.exec(
        select(AgentCapacityReservation)
        .where(
            or_(
                AgentCapacityReservation.session_id == agent.id,
                AgentCapacityReservation.local_session_id == agent.local_session_id,
            )
        )
        .with_for_update()
        .execution_options(populate_existing=True)
        .limit(2)
    ).all()
    if (
        owners != [agent.id]
        or len(turns) != 1
        or len(pending) != 1
        or len(permits) != 1
    ):
        raise ValueError("ambiguous_factory_drain")
    turn, message, permit = turns[0], pending[0], permits[0]
    if (
        turn.seq != 1
        or message.seq != turn.seq
        or permit.pending_seq != turn.seq
        or turn.terminal_reason != "interrupted_for_drain"
        or turn.stop_reason != "interrupted_for_drain"
        or turn.model != normalize_model(pin["model"])
        or message.model != normalize_model(pin["model"])
        or permit.model != normalize_model(pin["model"])
        or message.claimed_by_replica is not None
        or message.claimed_at is not None
        or message.partial_text is not None
        or message.partial_activities is not None
        or permit.session_id != agent.id
        or permit.local_session_id != agent.local_session_id
        or permit.state != "running"
        or permit.tier != "project"
        or permit.routine_job_name is not None
        or not permit.owner
        or permit.outcome is not None
        or permit.settled_at is not None
    ):
        raise ValueError("factory_drain_identity_changed")
    if type(message.dispatch_count) is not int or message.dispatch_count < 1:
        raise ValueError("missing_factory_dispatch_identity")
    if not isinstance(message.last_dispatch_at, datetime):
        raise ValueError("missing_factory_dispatch_identity")
    try:
        usage = json.loads(turn.usage_json or "{}")
    except (TypeError, ValueError) as exc:
        raise ValueError("missing_factory_dispatch_identity") from exc
    if (
        not isinstance(usage, dict)
        or usage.get("retry_dispatch_count") != message.dispatch_count
    ):
        raise ValueError("missing_factory_dispatch_identity")

    def stamp(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()

    dispatched_at = stamp(message.last_dispatch_at)
    interrupted_at = stamp(turn.created_at)
    if datetime.fromisoformat(dispatched_at) > datetime.fromisoformat(interrupted_at):
        raise ValueError("missing_factory_dispatch_identity")
    identity = {
        "session_id": agent.id,
        "local_session_id": agent.local_session_id,
        "workflow_id": agent.workflow_id,
        "guest_id": agent.ember_session_id,
        "lineage_id": agent.ember_lineage_id,
        "cli_session_id": agent.cli_session_id,
        "turn_id": turn.id,
        "pending_id": message.id,
        "permit_id": permit.id,
        "seq": turn.seq,
        "claim_owner": permit.owner,
        "dispatch_count": message.dispatch_count,
        "dispatched_at": dispatched_at,
        "interrupted_at": interrupted_at,
        "dispatch_id": exact_dispatch_id(
            agent.id,
            agent.ember_session_id,
            turn.seq,
            permit.owner,
            message.dispatch_count,
        ),
        "cost_usd": turn.cost_usd,
    }
    identity["identity_sha256"] = hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode()
    ).hexdigest()
    return identity


def settle_drained_lost_factory_attempt(db: Session, pin: dict, identity: dict) -> None:
    """Consume only the orphaned continuation after exact permanent loss."""
    from factory.execution.models import PendingMessage

    current = read_drained_lost_factory_attempt(db, pin, identity["session_id"])
    if current != identity:
        raise ValueError("factory_attempt_changed")
    agent = _locked_session(db, identity["session_id"])
    message = db.exec(
        select(PendingMessage)
        .where(PendingMessage.id == identity["pending_id"])
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    admission.settle(
        db,
        agent,
        identity["seq"],
        outcome="drained_guest_permanently_lost",
        cessation_confirmed=True,
    )
    # Preserve the durable turn, transcript handles and accounting history.
    # Only the now-dead active binding and its exact continuation are retired.
    agent.prior_ember_lineage_id = agent.ember_lineage_id
    agent.prior_cli_session_id = agent.cli_session_id
    _retire_cleanup_claim(agent)
    agent.ember_session_id = None
    agent.ember_session_token = None
    agent.ember_session_expires_at = None
    agent.ember_lineage_id = None
    agent.cli_session_id = None
    agent.progress_token = None
    agent.recovery_workspace_loss = True
    agent.status = "failed"
    db.add(agent)
    db.delete(message)
    db.flush()


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
    from factory.execution.models import PendingMessage

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
    from factory.execution.models import AgentTurn, PendingMessage

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
            usage_json=json.dumps(sanitize_payload(_cancelled_before_dispatch_usage())),
        )
    )
    owner.status = "warn"
    owner.last_turn_at = datetime.now(timezone.utc)
    db.add(owner)
    db.delete(row)
    db.flush()
    return owner.id


def _cancelled_before_dispatch_usage() -> dict:
    return {
        "source": "factory_reconciliation",
        "reason": "cancelled_before_dispatch",
    }


# Every permit outcome an observer can write for an attempt that lost its
# observer rather than its guest. _finish_unknown_locked (agent_sessions/
# store.py) passes the cause straight through to admission.settle, so this is
# the closed set of causes reachable with no binding: the shutdown release
# ("executor_cancelled" and the default "observer_released"), the two recovery
# sweeps ("unclaimed_attempt", "lease_expired"), the zombie reader
# ("zombie_observer_lost"). "delivery_error" is absent on purpose: it is written
# by mark_turn_error_sync, which leaves the session at "warn" with no stop
# reason, so it can never co-occur with the terminal status this proof
# requires. A cause
# outside this set is a shape this proof does not model, so it refuses.
# "factory_attempt_stop" is deliberately absent: that path requires an
# expected_guest_id, so a stopped attempt always had a guest.
_LOST_BEFORE_GUEST_OUTCOMES = frozenset(
    {
        "executor_cancelled",
        "lease_expired",
        "observer_released",
        "unclaimed_attempt",
        "zombie_observer_lost",
    }
)

# Every durable trace a live binding leaves behind, mirroring the same list in
# agent_sessions/permit_supervision.py. A guest that was bound and then cleared
# leaves one of these even when ember_session_id is back to NULL, and "the
# binding was cleared afterwards" is not the claim this proof makes.
_LOST_BEFORE_GUEST_BINDING_EVIDENCE = (
    "ember_session_id",
    "ember_session_token",
    "ember_session_expires_at",
    "ember_lineage_id",
    "prior_ember_lineage_id",
    "cli_session_id",
    "prior_cli_session_id",
    "guest_cleanup_id",
    "guest_cleanup_guest_id",
    "guest_cleanup_workflow_id",
    "guest_cleanup_dispatch_json",
    "guest_cleanup_started_at",
    "recovery_workspace_loss",
)


def inspect_lost_before_guest_factory_attempt(
    db: Session, pin: dict, session_id: int | None
) -> tuple[dict | None, str | None]:
    """Validate the session owner's invoked attempt that never bound a guest.

    Read this alongside read_not_invoked_factory_attempt: both settle a factory
    attempt with no remote side effect, and they are deliberately disjoint. The
    not-invoked proof covers a turn whose model POST was never set up, which
    the store records as a "warn" session with no stop_reason and a permit
    already settled "not_invoked". This proof covers the next window along: the
    turn WAS invoked, the executor was lost before it could bind a guest, and
    recovery recorded the ordinary unknown outcome. The session is "failed"
    with an UNKNOWN_INVOCATION turn and the permit is still "uncertain", so the
    not-invoked proof refuses it and stop supervision cannot start on it at
    all: read_uncertain_factory_attempt raises pending_or_missing_factory_guest
    without a binding, which is what leaves the attempt uncertain forever with
    the lane slot still held (#6025).

    There is no remote side effect to reconcile here. A guest is created before
    its binding is persisted and invoked only after (the on_create callback in
    agent_sessions/mcp.py), a result receipt is prepared only for a session
    whose ember_session_id already equals the guest it names (result_receipts.
    prepare_receipt), and a response-loss hold requires both that receipt and
    that guest id (store.mark_turn_response_lost_sync). So no binding, no
    receipt, no lineage and no cleanup claim together mean nothing was ever
    invoked on a guest: there is no CLI session to resume, no workspace to
    reconcile and no cessation to prove. The one window this cannot exclude is
    a guest created whose binding never committed, and such a guest was never
    invoked, holds no work and is reaped by Ember's own idle sweeper. What is
    settled here is the monolith-side reservation, never a guest.

    Fail-closed: every condition is required, and an unrecognised shape returns
    no proof. Returns (proof, None) or (None, the first failed condition), the
    name being what the operator path in
    factory/orchestration/factory_controls.py reports.
    Like the not-invoked proof this reads under the pool and session locks,
    settles nothing and clears nothing; settlement is a separate call.
    """
    from datetime import datetime, timezone
    from sqlalchemy import or_

    from factory.execution import normalize_model
    from factory.execution.constants import RESPONSE_LOST, UNKNOWN_INVOCATION
    from factory.execution.models import AgentResultReceipt, AgentTurn, PendingMessage

    if session_id is not None and (type(session_id) is not int or session_id < 1):
        raise ValueError("invalid factory session identity")
    admission.lock_pool(db)
    agent = _factory_owner(db, pin, session_id)
    if agent is None:
        return None, "missing_factory_owner"
    agent = _locked_session(db, agent.id)
    if _factory_owner(db, pin, agent.id) is None:
        return None, "factory_owner_changed"
    # "warn" is the not-invoked and delivery-error shape, owned by the other
    # proof. Only the store's terminal unknown-outcome status reaches here.
    if agent.status != "failed":
        return None, "session_not_terminal"
    if agent.result_receipt_fence_id is not None:
        return None, "result_receipt_fence_held"
    if admission.cleanup_pending(db, agent):
        return None, "guest_cleanup_pending"
    if (
        db.exec(
            select(PendingMessage.id).where(PendingMessage.session_id == agent.id)
        ).first()
        is not None
    ):
        return None, "pending_executor"
    if any(
        getattr(agent, name) is not None for name in _LOST_BEFORE_GUEST_BINDING_EVIDENCE
    ):
        return None, "prior_binding_evidence"
    turns = db.exec(
        select(AgentTurn).where(AgentTurn.session_id == agent.id).limit(2)
    ).all()
    if len(turns) != 1:
        return None, "ambiguous_turn_history"
    turn = turns[0]
    if (
        turn.seq != 1
        or turn.terminal_reason != "error"
        or turn.stop_reason != UNKNOWN_INVOCATION
        or turn.model != normalize_model(pin["model"])
    ):
        return None, "turn_not_lost_before_guest"
    # No guest means no work product and no measured spend. Any of these is
    # evidence that something ran, which this proof does not claim.
    if any(
        value is not None
        for value in (
            turn.cost_usd,
            turn.list_cost_usd,
            turn.commit_sha,
            turn.artifact_path,
            turn.artifact_blob,
            turn.artifact_outcome,
            turn.diff_blob,
        )
    ):
        return None, "turn_carries_execution_evidence"
    try:
        usage = json.loads(turn.usage_json or "{}")
    except (TypeError, ValueError):
        return None, "malformed_recovery"
    if not isinstance(usage, dict) or usage.get("activities"):
        return None, "malformed_recovery"
    recovery = usage.get("recovery")
    if not isinstance(recovery, dict) or not recovery:
        return None, "missing_recovery"
    # Mirrors _no_guest_delivery in agent_sessions/permit_supervision.py: a
    # recovery record that names a guest or a binding, or that carries streamed
    # output, describes a delivery this proof is not about.
    if any(
        "guest" in str(key).lower() or "binding" in str(key).lower() for key in recovery
    ):
        return None, "guest_delivery_evidence"
    if recovery.get("partial_text") or recovery.get("partial_activities"):
        return None, "guest_delivery_evidence"
    if recovery.get("invocation_phase") in {RESPONSE_LOST, "not_invoked"}:
        return None, "guest_delivery_evidence"
    if (
        type(recovery.get("dispatch_count")) is not int
        or recovery["dispatch_count"] < 1
        or not isinstance(recovery.get("claim_owner"), str)
        or not recovery["claim_owner"]
        or not isinstance(recovery.get("last_dispatch_at"), str)
    ):
        return None, "missing_dispatch_identity"
    try:
        dispatched = datetime.fromisoformat(
            recovery["last_dispatch_at"].replace("Z", "+00:00")
        )
    except (AttributeError, TypeError, ValueError):
        return None, "missing_dispatch_identity"

    def aware(value):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

    if not aware(dispatched) <= aware(turn.created_at):
        return None, "missing_dispatch_identity"
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
        return None, "ambiguous_permit"
    permit = permits[0]
    if (
        permit.session_id != agent.id
        or permit.local_session_id != agent.local_session_id
        or permit.pending_seq != 1
        or permit.tier != "project"
        or permit.routine_job_name is not None
        or permit.model != normalize_model(pin["model"])
        or permit.owner != recovery["claim_owner"]
    ):
        return None, "permit_ownership_conflict"
    if permit.state != "uncertain" or permit.settled_at is not None:
        return None, "permit_not_uncertain"
    # The cause the observer recorded and the outcome it settled the permit
    # with are written together, so a pair that disagrees is not this shape.
    if (
        permit.outcome not in _LOST_BEFORE_GUEST_OUTCOMES
        or recovery.get("cause") != permit.outcome
    ):
        return None, "unrecognised_outcome"
    # A receipt exists only for a session already bound to the guest it names,
    # so any receipt at all contradicts "no guest was ever bound". Read
    # metadata only, never a native result body.
    if (
        db.exec(
            select(AgentResultReceipt.id)
            .where(
                or_(
                    AgentResultReceipt.session_id == agent.id,
                    AgentResultReceipt.local_session_id == agent.local_session_id,
                )
            )
            .with_for_update()
        ).first()
        is not None
    ):
        return None, "guest_delivery_evidence"
    return {
        "session_id": agent.id,
        "local_session_id": agent.local_session_id,
        "workflow_id": agent.workflow_id,
        "turn_id": turn.id,
        "seq": 1,
        "dispatch_count": recovery["dispatch_count"],
        "claim_owner": permit.owner,
        "last_dispatch_at": recovery["last_dispatch_at"],
        "permit_id": permit.id,
        "permit_outcome": permit.outcome,
        "invocation_phase": "lost_before_guest",
        "cost_usd": 0.0,
    }, None


def read_lost_before_guest_factory_attempt(
    db: Session, pin: dict, session_id: int | None
) -> dict | None:
    """The proof from inspect_lost_before_guest_factory_attempt, or None.

    See that function for the evidence and why there is nothing remote to
    reconcile. This is the form the reconciler branch consults, alongside
    read_not_invoked_factory_attempt.
    """
    return inspect_lost_before_guest_factory_attempt(db, pin, session_id)[0]


def inspect_lost_before_session_factory_attempt(
    db: Session, pin: dict
) -> tuple[dict | None, str | None]:
    """Validate an attempt that reserved a start but never created a session.

    A reserved start is normally transient. Without the pinned turn-timeout
    age bound this proof could settle a live attempt while it is still creating
    its session. Once that strict bound has passed, the admitted run and exact
    reserved start must still carry no session, cost, outcome, deterministic
    session identity, capacity reservation, or result receipt. No session and
    no execution evidence mean no model call was dispatched.

    Fail closed: every condition is required, and an unrecognised shape returns
    no proof. Returns (proof, None) or (None, the first failed condition). Like
    the other factory proofs, this locks the capacity pool first, then reads the
    attempt records under row locks. Settlement is a separate call.
    """
    from factory.execution.models import AgentResultReceipt
    from factory.orchestration.factory_models import FactoryStart
    from factory.orchestration.models import SwarmNodeRun
    from factory.orchestration.node_workflows import (
        _session_key,
        resolve_node_session_id,
    )

    admission.lock_pool(db)
    run = db.exec(
        select(SwarmNodeRun)
        .where(
            SwarmNodeRun.task_id == pin["task_id"],
            SwarmNodeRun.node_key == pin["node_key"],
            SwarmNodeRun.attempt == pin["attempt"],
            SwarmNodeRun.dispatch_key == pin["workflow_id"],
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if run is None or run.status != "admitted":
        return None, "not_admitted"
    try:
        stored_pin = json.loads(run.pin_json or "null")
    except (TypeError, ValueError):
        return None, "invalid_attempt_pin"
    if stored_pin != pin or run.dispatch_key != pin.get("workflow_id"):
        return None, "attempt_ownership_conflict"
    if run.reserved_cost_usd != pin.get("max_cost_usd"):
        return None, "attempt_ownership_conflict"
    if run.session_id is not None:
        return None, "session_exists"
    if run.cost_usd is not None:
        return None, "carries_cost"
    if run.outcome_json not in (None, ""):
        return None, "carries_outcome"
    if (
        db.exec(
            select(SwarmNodeRun.id).where(
                SwarmNodeRun.task_id == pin["task_id"],
                SwarmNodeRun.node_key == pin["node_key"],
                SwarmNodeRun.attempt > pin["attempt"],
            )
        ).first()
        is not None
    ):
        return None, "newer_attempt_exists"

    start = db.exec(
        select(FactoryStart)
        .where(
            FactoryStart.task_id == pin["task_id"],
            FactoryStart.start_key == pin["workflow_id"],
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if start is None:
        return None, "missing_factory_start"
    if start.status != "reserved":
        return None, "start_not_reserved"
    if start.model != pin.get("model") or start.max_cost_usd != pin.get("max_cost_usd"):
        return None, "start_ownership_conflict"
    if start.session_id is not None:
        return None, "start_has_session"
    if resolve_node_session_id(pin, session=db) is not None:
        return None, "session_exists_deterministically"

    # Ask node_workflows for the key rather than restating its format: a
    # mismatch here would not fail, it would silently stop matching and leave
    # this capacity guard inert.
    local_session_id = _session_key(pin["task_id"], pin["node_key"], pin["attempt"])
    if (
        db.exec(
            select(AgentCapacityReservation.id)
            .where(AgentCapacityReservation.local_session_id == local_session_id)
            .with_for_update()
        ).first()
        is not None
    ):
        return None, "capacity_reserved"
    if (
        db.exec(
            select(AgentResultReceipt.id)
            .where(AgentResultReceipt.local_session_id == local_session_id)
            .with_for_update()
        ).first()
        is not None
    ):
        return None, "result_receipt_exists"

    def aware(value):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

    if (_utcnow() - aware(start.created_at)).total_seconds() <= pin[
        "turn_timeout_seconds"
    ]:
        return None, "start_too_recent"
    return {
        "session_id": None,
        "workflow_id": pin["workflow_id"],
        "start_id": start.id,
        "seq": 1,
        "cost_usd": 0.0,
        "invocation_phase": "never_dispatched",
    }, None


def settle_lost_before_session_factory_attempt(
    db: Session, pin: dict, proof: dict
) -> None:
    """Settle the FactoryStart and attempt records at zero cost.

    No guest, no session and no receipt means nothing was ever invoked on a
    remote side. Settle at zero cost and clear the reservation. The caller
    composes the graph, start and audit settlement in the same transaction.
    """
    from factory.orchestration.factory_models import FactoryStart

    current, _refusal = inspect_lost_before_session_factory_attempt(db, pin)
    if current != proof:
        raise ValueError("factory_attempt_changed")
    start = db.exec(
        select(FactoryStart)
        .where(FactoryStart.id == proof["start_id"])
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    start.status = "failed"
    start.cost_usd = 0.0
    start.accounting_basis = "no_model_post"
    db.add(start)


def settle_lost_before_guest_factory_attempt(
    db: Session, pin: dict, proof: dict
) -> None:
    """Release the permit of an attempt proven to have never bound a guest.

    Re-reads the proof under the caller's locks and refuses a changed attempt,
    the way settle_uncertain_factory_attempt does. Cessation is confirmed
    rather than assumed: with no guest, no CLI session and no receipt there is
    nothing whose cessation could be outstanding, so the reservation is settled
    here rather than held for evidence that can never arrive. The failed turn
    and its UNKNOWN_INVOCATION marker stay immutable; the caller composes the
    graph, start and audit settlement in this same transaction.
    """
    current = read_lost_before_guest_factory_attempt(db, pin, proof["session_id"])
    if current != proof:
        raise ValueError("factory_attempt_changed")
    agent = _locked_session(db, proof["session_id"])
    admission.settle(
        db,
        agent,
        proof["seq"],
        outcome="lost_before_guest",
        cessation_confirmed=True,
    )
    db.add(agent)
