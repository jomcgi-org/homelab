from __future__ import annotations

import base64
import binascii
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, exists, func, or_, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased
from sqlmodel import Session, select

from agent_sessions.constants import (
    CLEAN_TERMINAL_REASONS,
    INTERRUPTED_TERMINAL_REASONS,
    LEGACY_QWEN_SYNTHETIC_PROMPT,
    SYNTHETIC_SESSION_PREFIX,
)
from agent_sessions.models import (
    AgentSession,
    AgentTurn,
    PendingMessage,
    VoiceUICompanion,
    VoiceUILedger,
)
from agent_sessions.transport import Turn
from core.db import get_engine

logger = logging.getLogger(__name__)

_UNSET = object()
RECLAIM_LEASE = timedelta(seconds=30)
MAX_PENDING_DISPATCHES = 3
UNKNOWN_INVOCATION = "invocation_outcome_unknown"
UNKNOWN_INVOCATION_MESSAGE = (
    "This session has an unknown invocation outcome. Reconcile the guest and any "
    "remote side effects, then start a new session. Sending again cannot resume it."
)


class SessionOutcomeUnknown(ValueError):
    """The session is held for operator reconciliation."""


class PendingClaimLost(RuntimeError):
    """An executor no longer owns the attempt it is trying to finish."""


def _unknown_outcome_exists(session_id_column):
    return exists().where(
        AgentTurn.session_id == session_id_column,
        AgentTurn.stop_reason == UNKNOWN_INVOCATION,
    )


def _lock_session(session: Session, session_id: int) -> AgentSession | None:
    """Serialize lane writes, including on SQLite where FOR UPDATE is ignored.

    Always lock the session before its pending row. The no-op UPDATE acquires a
    database write lock without changing user-visible timestamps.
    """
    session.execute(
        update(AgentSession)
        .where(AgentSession.id == session_id)
        .values(last_turn_at=AgentSession.last_turn_at)
    )
    return session.get(AgentSession, session_id, populate_existing=True)


def _assert_sendable(session: Session, session_id: int) -> None:
    if session.exec(select(_unknown_outcome_exists(session_id))).one():
        raise SessionOutcomeUnknown(UNKNOWN_INVOCATION_MESSAGE)


def _attempted(pending: PendingMessage) -> bool:
    # A claim is conservative dispatch evidence, not a count of provider calls.
    # Progress also protects rows created before dispatch tracing was added.
    return bool(
        pending.dispatch_count
        or pending.last_dispatch_at
        or pending.claimed_by_replica
        or pending.partial_text
        or pending.partial_activities
    )


def _progress_usage(pending: PendingMessage, cause: str) -> dict:
    try:
        activities = json.loads(pending.partial_activities or "[]")
    except (TypeError, ValueError):
        activities = []
    return {
        "activities": activities,
        "recovery": {
            "cause": cause,
            "dispatch_count": pending.dispatch_count,
            "last_dispatch_at": (
                pending.last_dispatch_at.isoformat()
                if pending.last_dispatch_at
                else None
            ),
            "claim_owner": pending.claimed_by_replica,
            "partial_text": pending.partial_text,
            "partial_activities": pending.partial_activities,
        },
    }


def _retry_permission(turn: AgentTurn | None, pending: PendingMessage) -> bool:
    if turn is None or turn.terminal_reason not in INTERRUPTED_TERMINAL_REASONS:
        return False
    try:
        usage = json.loads(turn.usage_json or "{}")
    except (TypeError, ValueError):
        return False
    return (
        isinstance(usage, dict)
        and turn.stop_reason == "brick_preempted"
        and pending.dispatch_count > 0
        and usage.get("retry_dispatch_count") == pending.dispatch_count
    )


def _finish_unknown_locked(
    session: Session, row: AgentSession, pending: PendingMessage, cause: str
) -> None:
    """Record evidence and consume the attempted head in the caller's transaction."""
    existing = get_turn(session, row.id, pending.seq)
    if (
        existing is not None
        and existing.terminal_reason not in INTERRUPTED_TERMINAL_REASONS
    ):
        # A completed result may have committed before an old executor's cleanup.
        session.delete(pending)
        return
    usage = _progress_usage(pending, cause)
    if existing is not None:
        usage["prior_interruption"] = {
            "result_text": existing.result_text,
            "usage_json": existing.usage_json,
        }
        session.delete(existing)
        session.flush()
    session.add(
        AgentTurn(
            session_id=row.id,
            seq=pending.seq,
            prompt=pending.message_text,
            model=pending.model,
            voice_summary=UNKNOWN_INVOCATION_MESSAGE,
            result_text=pending.partial_text or UNKNOWN_INVOCATION_MESSAGE,
            terminal_reason="error",
            stop_reason=UNKNOWN_INVOCATION,
            permission_denials="[]",
            usage_json=json.dumps(usage),
            cost_usd=None,
        )
    )
    row.status = "failed"
    row.last_turn_at = datetime.now(timezone.utc)
    row.voice_summary = UNKNOWN_INVOCATION_MESSAGE
    # Leave the guest and lineage handles available for operator inspection.
    row.progress_token = None
    row.recovery_workspace_loss = None
    session.add(row)
    session.delete(pending)


def finish_unknown_pending_sync(
    session_id: int,
    turn_seq: int,
    claim_owner: str,
    dispatch_count: int,
    cause: str,
) -> bool:
    with Session(get_engine()) as session:
        row = _lock_session(session, session_id)
        pending = get_pending_message(session, session_id, turn_seq)
        if (
            row is None
            or pending is None
            or pending.claimed_by_replica != claim_owner
            or pending.dispatch_count != dispatch_count
        ):
            return False
        _finish_unknown_locked(session, row, pending, cause)
        session.commit()
        return True


def create_voice_ui_companion(
    session: Session,
    companion_id: str,
    principal_subject: str,
    principal_authority: str,
    now: datetime,
) -> VoiceUICompanion:
    row = VoiceUICompanion(
        id=companion_id,
        principal_subject=principal_subject,
        principal_authority=principal_authority,
        created_at=now,
        last_seen_at=now,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def get_voice_ui_companion(
    session: Session, companion_id: str
) -> VoiceUICompanion | None:
    return session.get(VoiceUICompanion, companion_id)


def heartbeat_voice_ui_companion(
    session: Session,
    companion: VoiceUICompanion,
    now: datetime,
    principal_subject: str,
    principal_authority: str,
) -> VoiceUICompanion:
    companion.last_seen_at = now
    companion.principal_subject = principal_subject
    companion.principal_authority = principal_authority
    session.add(companion)
    session.commit()
    session.refresh(companion)
    return companion


def get_open_voice_ui_companion(
    session: Session, now: datetime, *, for_update: bool = False
) -> VoiceUICompanion | None:
    statement = (
        select(VoiceUICompanion)
        .where(
            VoiceUICompanion.closed_at.is_(None),
            VoiceUICompanion.last_seen_at >= now - timedelta(seconds=90),
        )
        .order_by(
            VoiceUICompanion.last_seen_at.desc(), VoiceUICompanion.created_at.desc()
        )
        .limit(1)
    )
    if for_update:
        statement = statement.with_for_update()
    return session.exec(statement).first()


def record_voice_ui_call(
    session: Session,
    companion: VoiceUICompanion,
    call: str,
    payload: dict,
    principal_subject: str,
    principal_authority: str,
    *,
    bound_session_id: int | None | object = _UNSET,
) -> VoiceUILedger:
    if bound_session_id is not _UNSET:
        companion.session_id = bound_session_id
    row = VoiceUILedger(
        companion_id=companion.id,
        session_id=companion.session_id,
        call=call,
        payload=payload,
        principal_subject=principal_subject,
        principal_authority=principal_authority,
    )
    session.add_all([companion, row])
    session.commit()
    session.refresh(row)
    return row


def poll_voice_ui_ledger(
    session: Session, companion_id: str, since: int, now: datetime
) -> list[dict] | None:
    companion = get_voice_ui_companion(session, companion_id)
    if companion is None:
        return None
    if companion.closed_at is None:
        companion.last_seen_at = now
        session.add(companion)
    rows = list(
        session.exec(
            select(VoiceUILedger)
            .where(
                VoiceUILedger.companion_id == companion_id,
                VoiceUILedger.id > since,
            )
            .order_by(VoiceUILedger.id)
        ).all()
    )
    payloads = [
        {
            "id": row.id,
            "companion_id": row.companion_id,
            "session_id": row.session_id,
            "call": row.call,
            "payload": row.payload,
            "principal_subject": row.principal_subject,
            "principal_authority": row.principal_authority,
            "created_at": row.created_at,
        }
        for row in rows
    ]
    session.commit()
    return payloads


def create_session(
    session: Session,
    local_session_id: str,
    workspace: str,
    branch: str,
    model: str | None = None,
    repo: str | None = None,
    *,
    discord_thread: str | None = None,
    system_prompt: str | None = None,
    reasoning: bool = False,
    workflow_id: str | None = None,
    triggered_by: str | None = None,
    node_key: str | None = None,
    node_attempt: int | None = None,
) -> AgentSession:
    row = AgentSession(
        local_session_id=local_session_id,
        workspace=workspace,
        branch=branch,
        repo=repo,
        discord_thread=discord_thread,
        model=model,
        progress_token=secrets.token_urlsafe(32),
        system_prompt=system_prompt,
        reasoning=reasoning,
        workflow_id=workflow_id,
        node_key=node_key,
        node_attempt=node_attempt,
        # Collapses whitespace-only to None rather than "". An empty string is a
        # third state that reads as "owned by nobody": it passes a NULL check but
        # matches no caller, so it would silently create rows nobody can ever read.
        triggered_by=(triggered_by or "").strip().lower() or None,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def get_session(session: Session, session_id: int) -> AgentSession | None:
    return session.get(AgentSession, session_id)


def get_session_by_local_id(session: Session, local_id: str) -> AgentSession | None:
    return session.exec(
        select(AgentSession).where(AgentSession.local_session_id == local_id)
    ).first()


def sessions_for_workflow(session: Session, workflow_id: str) -> list[AgentSession]:
    return list(
        session.exec(
            select(AgentSession).where(AgentSession.workflow_id == workflow_id)
        ).all()
    )


def get_session_by_discord_thread(
    session: Session, thread_id: str
) -> AgentSession | None:
    return session.exec(
        select(AgentSession).where(AgentSession.discord_thread == thread_id)
    ).first()


def set_ember_session(
    session: Session,
    session_id: int,
    ember_id: str,
    ember_token: str,
    ember_expires_at: int | None,
    ember_lineage_id: str | None = None,
    cli_session_id: str | None | object = _UNSET,
    is_restored: bool = False,
) -> AgentSession:
    row = _lock_session(session, session_id)
    _assert_sendable(session, session_id)
    if row is None:
        raise ValueError(f"Unknown agent session {session_id}")
    row.ember_session_id = ember_id
    row.ember_session_token = ember_token
    row.ember_session_expires_at = ember_expires_at
    # #4306 slice 4: the durable workspace handle, persisted alongside the
    # per-generation session_id/token so the NEXT create (after an expiry)
    # can restore from it instead of the (invalid, past generation zero)
    # session_id.
    row.ember_lineage_id = ember_lineage_id
    if cli_session_id is not _UNSET:
        row.cli_session_id = cli_session_id
    # A blank fallback must preserve the durable restore handle until the
    # replacement turn is known good.
    if is_restored:
        row.prior_ember_lineage_id = None
        row.prior_cli_session_id = None
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def replace_ember_session_after_preemption(
    session: Session,
    session_id: int,
    ember_id: str,
    ember_token: str,
    ember_expires_at: int | None,
    ember_lineage_id: str | None,
) -> AgentSession:
    """Persist a blank replacement while retaining the preempted generation."""
    row = _lock_session(session, session_id)
    _assert_sendable(session, session_id)
    if row is None:
        raise ValueError(f"Unknown agent session {session_id}")
    if row.ember_lineage_id:
        row.prior_ember_lineage_id = row.ember_lineage_id
    if row.cli_session_id:
        row.prior_cli_session_id = row.cli_session_id
    row.ember_session_id = ember_id
    row.ember_session_token = ember_token
    row.ember_session_expires_at = ember_expires_at
    row.ember_lineage_id = ember_lineage_id
    row.cli_session_id = None
    row.recovery_workspace_loss = True
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def clear_ember_session(session: Session, session_id: int) -> AgentSession:
    """Clear EmberVM session identity and CLI session id together.

    The VM and its CLI transcript are one unit of state; both must be cleared
    or neither, else the next turn attempts --resume on a fresh VM (503).

    #4306 slice 5: before nulling, the ACTIVE lineage/CLI transcript are
    copied to prior_ember_lineage_id/prior_cli_session_id, so the next send
    can still restore the durable workspace even through this
    confirmed-dead-binding path (EmberSessionGone: a 410/403 AND its retry
    also failed), which otherwise drops the lineage handle entirely and
    starts a blank conversation despite the S3 workspace still existing.
    Only overwrites prior_* when the ACTIVE value is non-nil: a repeated
    clear on an already-blank binding must not wipe out a good prior with a
    nil.
    """
    row = session.get(AgentSession, session_id)
    if row is None:
        raise ValueError(f"Unknown agent session {session_id}")
    if row.ember_lineage_id:
        row.prior_ember_lineage_id = row.ember_lineage_id
    if row.cli_session_id:
        row.prior_cli_session_id = row.cli_session_id
    row.ember_session_id = None
    row.ember_session_token = None
    row.ember_session_expires_at = None
    row.ember_lineage_id = None
    row.cli_session_id = None
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def clear_ember_bindings_by_ember_id(session: Session, ember_id: str) -> list[int]:
    """Null the ember binding on every AgentSession bound to ember_id.

    A destroyed EmberVM session can be bound to more than one AgentSession
    row over its lifetime (retries, resumed turns), so this clears all of
    them in one commit rather than assuming a single owner. Rows are loaded
    via exec/select and mutated in place, then committed once; nothing is
    session.add-ed in a loop.

    #4306 slice 5: mirrors clear_ember_session's prior_* preservation (see
    there for the non-nil-guard rationale), so an admin destroy ALSO
    survives as a restorable lineage. This supersedes Slice 4's original
    carve-out here (which left ember_lineage_id/cli_session_id untouched,
    reasoning the destroyed VM's volume might still be restorable from the
    still-live ember_lineage_id): mcp.py's restore check now looks at
    prior_ember_lineage_id uniformly for BOTH failure paths, so leaving this
    one still populating the ACTIVE (not prior) field would make it
    invisible to that check.

    Returns the ids of the affected AgentSession rows.
    """
    rows = session.exec(
        select(AgentSession).where(AgentSession.ember_session_id == ember_id)
    ).all()
    ids: list[int] = []
    for row in rows:
        if row.ember_lineage_id:
            row.prior_ember_lineage_id = row.ember_lineage_id
        if row.cli_session_id:
            row.prior_cli_session_id = row.cli_session_id
        row.ember_session_id = None
        row.ember_session_token = None
        row.ember_session_expires_at = None
        row.ember_lineage_id = None
        row.cli_session_id = None
        ids.append(row.id)
    session.commit()
    return ids


def create_turn(
    session: Session,
    session_id: int,
    seq: int,
    prompt: str,
    voice_summary: str | None,
    result_text: str,
    terminal_reason: str | None,
    stop_reason: str | None,
    permission_denials: list | None,
    commit_sha: str | None,
    usage: dict | None,
    cost_usd: float | None,
    cli_session_id: str | None = None,
    model: str | None = None,
    prompt_intent: str | None = None,
    diff_blob: bytes | None = None,
    diff_truncated: bool = False,
    diff_base_sha: str | None = None,
    artifact_path: str | None = None,
    artifact_blob: bytes | None = None,
    artifact_outcome: str | None = None,
    *,
    commit: bool = True,
) -> AgentTurn:
    row = AgentTurn(
        session_id=session_id,
        seq=seq,
        model=model,
        prompt=prompt,
        prompt_intent=prompt_intent,
        voice_summary=voice_summary,
        result_text=result_text,
        terminal_reason=terminal_reason,
        stop_reason=stop_reason,
        permission_denials=json.dumps(permission_denials or []),
        commit_sha=commit_sha,
        diff_blob=diff_blob,
        diff_truncated=diff_truncated,
        diff_base_sha=diff_base_sha,
        artifact_path=artifact_path,
        artifact_blob=artifact_blob,
        artifact_outcome=artifact_outcome,
        usage_json=json.dumps(usage or {}),
        cost_usd=cost_usd,
    )
    session.add(row)
    if commit:
        session.commit()
        session.refresh(row)
    else:
        session.flush()
    return row


def _no_live_pending_claim(session_id_column, now: datetime):
    """Return the shared lane invariant excluding a live executor claim."""
    claimed = aliased(PendingMessage)
    live_claim_cutoff = now - RECLAIM_LEASE
    return ~exists().where(
        claimed.session_id == session_id_column,
        claimed.claimed_by_replica.isnot(None),
        claimed.claimed_at.isnot(None),
        claimed.claimed_at > live_claim_cutoff,
    )


def _lane_head_id(session_id_column):
    pending = aliased(PendingMessage)
    return (
        select(pending.id)
        .where(pending.session_id == session_id_column)
        .order_by(pending.seq)
        .limit(1)
        .correlate(AgentSession)
        .scalar_subquery()
    )


def _recovery_candidate_predicate(cutoff: datetime, now: datetime):
    head = aliased(PendingMessage)
    turn_exists = exists().where(AgentTurn.session_id == AgentSession.id)
    old_lane_head_exists = exists().where(
        head.id == _lane_head_id(AgentSession.id),
        head.created_at <= cutoff,
    )
    return and_(
        AgentSession.status == "running",
        AgentSession.recovery_completed_at.is_(None),
        ~turn_exists,
        old_lane_head_exists,
        _no_live_pending_claim(AgentSession.id, now),
    )


def _hung_claim_candidate_predicate(
    cutoff: datetime,
    now: datetime,
    expected_ember_session_id: str | None = None,
):
    """Match a live lane-head claim whose dispatch began before cutoff."""
    head = aliased(PendingMessage)
    turn_exists = exists().where(AgentTurn.session_id == AgentSession.id)
    live_claim_cutoff = now - RECLAIM_LEASE
    hung_lane_head_exists = exists().where(
        head.id == _lane_head_id(AgentSession.id),
        head.claimed_by_replica.isnot(None),
        head.claimed_at.isnot(None),
        head.claimed_at > live_claim_cutoff,
        head.last_dispatch_at.isnot(None),
        head.last_dispatch_at < cutoff,
    )
    clauses = [
        AgentSession.status == "running",
        AgentSession.ember_session_id.isnot(None),
        ~turn_exists,
        hung_lane_head_exists,
    ]
    if expected_ember_session_id is not None:
        clauses.append(AgentSession.ember_session_id == expected_ember_session_id)
    return and_(*clauses)


def _recovery_workspace_loss(row: AgentSession, pending: PendingMessage) -> bool:
    """Decide once, at claim time, whether recovery may lose workspace state."""
    guest_may_have_run = bool(row.ember_session_id or pending.claimed_by_replica)
    return row.repo is not None and guest_may_have_run


def find_zombie_session_ids(
    session: Session, cutoff: datetime, now: datetime, limit: int = 5
) -> list[int]:
    """Find a bounded batch of old, unexecuted running lane heads.

    Control-plane-confirmed hung claims are collected separately because the
    database predicate alone is not enough to authorize recovery.
    """
    return list(
        session.exec(
            select(AgentSession.id)
            .where(_recovery_candidate_predicate(cutoff, now))
            .order_by(AgentSession.id)
            .limit(limit)
        ).all()
    )


def find_hung_claim_session_ids(
    session: Session, cutoff: datetime, now: datetime, limit: int = 5
) -> list[int]:
    """Find a bounded batch of live claims older than the hung threshold."""
    return list(
        session.exec(
            select(AgentSession.id)
            .where(_hung_claim_candidate_predicate(cutoff, now))
            .order_by(AgentSession.id)
            .limit(limit)
        ).all()
    )


def get_hung_claim_binding(
    session: Session, session_id: int, cutoff: datetime, now: datetime
) -> str | None:
    """Return the current binding only while the hung database shape holds."""
    return session.exec(
        select(AgentSession.ember_session_id).where(
            AgentSession.id == session_id,
            _hung_claim_candidate_predicate(cutoff, now),
        )
    ).first()


def _claim_zombie_session_recovery(
    session: Session,
    session_id: int,
    now: datetime,
    predicate,
    *,
    steal_claim: bool,
) -> dict | None:
    """CAS one zombie shape into the shared recovery state."""
    # Acquire the same lock as heartbeats before evaluating pending predicates.
    # PostgreSQL may evaluate a subquery before an UPDATE waits for a row lock.
    _lock_session(session, session_id)
    result = session.execute(
        update(AgentSession)
        .where(AgentSession.id == session_id, predicate)
        .values(status="recovering", last_turn_at=now)
    )
    if result.rowcount != 1:
        session.rollback()
        return None

    row = session.get(AgentSession, session_id, populate_existing=True)
    pending = session.exec(
        select(PendingMessage)
        .where(PendingMessage.session_id == session_id)
        .order_by(PendingMessage.seq)
    ).first()
    if row is None or pending is None:
        # The lane head can disappear after the CAS predicate is evaluated.
        # Revert in this transaction so no abandoned claim strands recovering.
        session.execute(
            update(AgentSession)
            .where(
                AgentSession.id == session_id,
                AgentSession.status == "recovering",
            )
            .values(status="running", recovery_workspace_loss=None)
        )
        session.commit()
        return None

    claim = {
        "session_id": session_id,
        "turn_seq": pending.seq,
        "message_text": pending.message_text,
        "model": pending.model,
        "ember_session_id": row.ember_session_id,
        "recovery_workspace_loss": _recovery_workspace_loss(row, pending),
    }
    if _attempted(pending) or row.ember_session_id is not None:
        _finish_unknown_locked(session, row, pending, "zombie_observer_lost")
        session.commit()
        return {**claim, "outcome_unknown": True}
    if steal_claim:
        # This is the sole recovery path allowed to revoke a live executor's
        # lease. refresh_claim_sync checks claimed_by_replica and will make the
        # hung executor abort before it can persist a result.
        pending.claimed_by_replica = None
        pending.claimed_at = None

    row.recovery_workspace_loss = claim["recovery_workspace_loss"]
    session.add_all([row, pending])
    session.commit()
    return claim


def claim_zombie_session_recovery(
    session: Session,
    session_id: int,
    cutoff: datetime,
    now: datetime,
) -> dict | None:
    """CAS a first-turn zombie into a recovery lease.

    The update predicate repeats the detection conditions. This makes the
    status transition the cross-pod arbiter instead of relying on a preceding
    read that can race another reconciler.
    """
    return _claim_zombie_session_recovery(
        session,
        session_id,
        now,
        _recovery_candidate_predicate(cutoff, now),
        steal_claim=False,
    )


def claim_hung_zombie_session_recovery(
    session: Session,
    session_id: int,
    cutoff: datetime,
    now: datetime,
    expected_ember_session_id: str,
) -> dict | None:
    """CAS a CP-confirmed hung claim and revoke its executor lease."""
    return _claim_zombie_session_recovery(
        session,
        session_id,
        now,
        _hung_claim_candidate_predicate(cutoff, now, expected_ember_session_id),
        steal_claim=True,
    )


def finalize_zombie_session_recovery(
    session: Session, claim: dict, now: datetime
) -> int | None:
    """Release the original lane head in place after clearing its binding."""
    session_id = claim["session_id"]
    turn_seq = claim["turn_seq"]
    row = _lock_session(session, session_id)
    pending = get_pending_message(session, session_id, turn_seq)
    if row is None or row.status != "recovering" or pending is None:
        session.rollback()
        return None
    if (
        _attempted(pending)
        or row.ember_session_id is not None
        or get_turn(session, session_id, turn_seq) is not None
    ):
        session.rollback()
        return None

    pending.claimed_by_replica = None
    pending.claimed_at = None
    row.status = "running"
    row.last_turn_at = now
    row.recovery_workspace_loss = None
    row.recovery_completed_at = now
    session.add_all([row, pending])
    session.commit()
    return turn_seq


def update_session_status(
    session: Session, session_id: int, status: str, voice_summary: str | None = None
) -> AgentSession:
    row = _lock_session(session, session_id)
    if row is None:
        raise ValueError(f"Unknown agent session {session_id}")
    if session.exec(select(_unknown_outcome_exists(session_id))).one():
        status = "failed"
        voice_summary = UNKNOWN_INVOCATION_MESSAGE
    row.status = status
    row.last_turn_at = datetime.now(timezone.utc)
    if voice_summary is not None:
        row.voice_summary = voice_summary
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def activate_session_after_enqueue(session: Session, session_id: int) -> bool:
    """Set running unless recovery currently owns the session lane."""
    _lock_session(session, session_id)
    result = session.execute(
        update(AgentSession)
        .where(
            AgentSession.id == session_id,
            AgentSession.status != "recovering",
            ~_unknown_outcome_exists(AgentSession.id),
        )
        .values(status="running", last_turn_at=datetime.now(timezone.utc))
    )
    session.commit()
    return result.rowcount == 1


def get_turns(session: Session, session_id: int) -> list[AgentTurn]:
    return list(
        session.exec(
            select(AgentTurn)
            .where(AgentTurn.session_id == session_id)
            .order_by(AgentTurn.seq)
        ).all()
    )


def get_turn(session: Session, session_id: int, turn_seq: int) -> AgentTurn | None:
    return session.exec(
        select(AgentTurn).where(
            AgentTurn.session_id == session_id, AgentTurn.seq == turn_seq
        )
    ).first()


def update_turn_shas(
    session: Session,
    session_id: int,
    turn_seq: int,
    base_sha: str | None,
    commit_sha: str | None,
) -> AgentTurn | None:
    """Attach branch heads to a turn after the swarm observes its completion."""
    row = get_turn(session, session_id, turn_seq)
    if row is None:
        return None
    row.base_sha = base_sha
    row.commit_sha = commit_sha
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def update_turn_prompt_intent(
    session: Session,
    session_id: int,
    turn_seq: int,
    prompt_intent: str | None,
) -> AgentTurn | None:
    """Attach the server-owned intent to a persisted turn."""
    row = get_turn(session, session_id, turn_seq)
    if row is None:
        return None
    row.prompt_intent = prompt_intent
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def lexical_search(session: Session, query_text: str, limit: int = 20) -> list[dict]:
    """Search agent turns and return ranked session/turn result dictionaries."""
    if not query_text or not query_text.strip():
        return []

    sql = text(
        "WITH q AS (SELECT websearch_to_tsquery('english', :q) AS q) "
        "SELECT ranked.session_id, ranked.local_session_id, ranked.workspace, "
        "ranked.seq, ranked.created_at, ranked.rank, "
        "ts_headline('english', ranked.prompt || ' ' || ranked.result_text, "
        "ranked.query, 'MaxWords=20,MinWords=3,ShortWord=0,StartSel=,StopSel=') "
        "AS snippet FROM ("
        "SELECT t.session_id, s.local_session_id, s.workspace, t.seq, "
        "t.created_at, t.prompt, t.result_text, q.q AS query, "
        "ts_rank_cd(t.fts_vector, q.q) AS rank "
        "FROM agent_sessions.agent_turns t "
        "JOIN agent_sessions.agent_sessions s ON t.session_id = s.id, q "
        "WHERE t.fts_vector @@ q.q "
        "AND s.local_session_id NOT LIKE :synthetic_prefix "
        "AND NOT (COALESCE(s.model, '') = 'qwen' AND COALESCE(("
        "SELECT first_turn.prompt FROM agent_sessions.agent_turns first_turn "
        "WHERE first_turn.session_id = s.id ORDER BY first_turn.seq LIMIT 1"
        "), '') = :qwen_synthetic_prompt) "
        "ORDER BY rank DESC, t.created_at DESC LIMIT :limit"
        ") AS ranked"
    )
    result = session.exec(
        sql,
        params={
            "q": query_text,
            "limit": limit,
            "synthetic_prefix": f"{SYNTHETIC_SESSION_PREFIX}%",
            "qwen_synthetic_prompt": LEGACY_QWEN_SYNTHETIC_PROMPT,
        },
    )
    keys = (
        "session_id",
        "local_session_id",
        "workspace",
        "seq",
        "created_at",
        "rank",
        "snippet",
    )
    rows: list[dict] = []
    for row in result:
        mapping = getattr(row, "_mapping", None)
        rows.append(dict(mapping) if mapping is not None else dict(zip(keys, row)))
    return rows


def create_pending_message(
    session: Session, session_id: int, message_text: str, model: str | None = None
) -> PendingMessage:
    """Enqueue a message durably and return its per-session turn sequence.

    The seq is allocated optimistically and retried on collision. Multiple
    concurrent writers may compute the same seq and INSERT it. The UNIQUE
    constraint on (session_id, seq) is the arbiter: one succeeds, the rest
    receive IntegrityError. On collision, rollback, recompute the seq from
    scratch, and retry (up to 5 attempts). After all attempts are exhausted,
    raise RuntimeError.
    """
    if _lock_session(session, session_id) is None:
        raise ValueError(f"Unknown agent session {session_id}")
    _assert_sendable(session, session_id)

    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            last_turn = session.exec(
                select(func.max(AgentTurn.seq)).where(
                    AgentTurn.session_id == session_id
                )
            ).one()
            last_pending = session.exec(
                select(func.max(PendingMessage.seq)).where(
                    PendingMessage.session_id == session_id
                )
            ).one()
            seq = max(last_turn or 0, last_pending or 0) + 1
            row = PendingMessage(
                session_id=session_id,
                seq=seq,
                model=model,
                message_text=message_text,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row
        except IntegrityError:
            session.rollback()
            if attempt == max_attempts - 1:
                raise RuntimeError(
                    f"Failed to allocate seq for session {session_id} after {max_attempts} attempts"
                )
            _lock_session(session, session_id)
            _assert_sendable(session, session_id)


def get_pending_message(
    session: Session, session_id: int, turn_seq: int
) -> PendingMessage | None:
    return session.exec(
        select(PendingMessage).where(
            PendingMessage.session_id == session_id,
            PendingMessage.seq == turn_seq,
        )
    ).first()


def get_pending_message_sync(session_id: int, turn_seq: int) -> PendingMessage | None:
    """Fetch one pending message using a fresh synchronous database session."""
    with Session(get_engine()) as session:
        return get_pending_message(session, session_id, turn_seq)


def write_progress_sync(
    progress_token: str,
    partial_text: str,
    activities: list | None = None,
) -> str:
    """Write progress only to a claimed turn in an unfrozen session."""
    with Session(get_engine()) as session:
        session_id = session.exec(
            select(AgentSession.id).where(AgentSession.progress_token == progress_token)
        ).first()
        if session_id is None:
            return "unknown_token"
        row = _lock_session(session, session_id)
        if row is None or row.progress_token != progress_token:
            return "unknown_token"
        try:
            _assert_sendable(session, session_id)
        except SessionOutcomeUnknown:
            return "unknown_token"
        pending = session.exec(
            select(PendingMessage)
            .where(
                PendingMessage.session_id == session_id,
                PendingMessage.claimed_by_replica.isnot(None),
            )
            .order_by(PendingMessage.seq)
        ).first()
        if pending is None:
            return "no_row"
        pending.partial_text = partial_text
        if activities is not None:
            pending.partial_activities = json.dumps(activities)
        session.add(pending)
        session.commit()
        return "ok"


def _persist_progress_token_sync(session_id: int, progress_token: str) -> None:
    """Mint and persist a progress token for a pre-migration session."""
    with Session(get_engine()) as session:
        row = _lock_session(session, session_id)
        _assert_sendable(session, session_id)
        if row is not None and row.progress_token is None:
            row.progress_token = progress_token
            session.add(row)
            session.commit()


def claim_pending_message_for_session_sync(
    session_id: int, replica_id: str
) -> int | None:
    """Claim the lane head once, or consume one exact preemption retry grant."""
    with Session(get_engine()) as session:
        row = _lock_session(session, session_id)
        if row is None or row.status in {"awaiting_login", "failed"}:
            return None
        try:
            _assert_sendable(session, session_id)
        except SessionOutcomeUnknown:
            return None
        pending = session.exec(
            select(PendingMessage)
            .where(PendingMessage.session_id == session_id)
            .order_by(PendingMessage.seq)
        ).first()
        if pending is None or pending.claimed_by_replica is not None:
            return None
        previous = get_turn(session, session_id, pending.seq)
        if _attempted(pending):
            if not _retry_permission(previous, pending):
                _finish_unknown_locked(session, row, pending, "unclaimed_attempt")
                session.commit()
                return None
            # Consume permission before the next attempt starts. If this attempt
            # loses its observer, the previous interrupted row cannot authorize it.
            usage = json.loads(previous.usage_json)
            usage.pop("retry_dispatch_count")
            previous.usage_json = json.dumps(usage)
            session.add(previous)
        elif row.status == "recovering":
            return None
        seq = pending.seq
        pending.claimed_by_replica = replica_id
        pending.claimed_at = func.now()
        pending.dispatch_count += 1
        pending.last_dispatch_at = func.now()
        row.recovery_completed_at = None
        session.add_all([row, pending])
        session.commit()
        return seq


def release_pending_message_claim_sync(
    session_id: int,
    turn_seq: int,
    replica_id: str,
    cause: str = "observer_released",
) -> bool:
    """Release only a known preemption; observer loss is an unknown outcome."""
    with Session(get_engine()) as session:
        row = _lock_session(session, session_id)
        pending = get_pending_message(session, session_id, turn_seq)
        if row is None:
            return False
        if pending is None or pending.claimed_by_replica != replica_id:
            return session.exec(select(_unknown_outcome_exists(session_id))).one()
        previous = get_turn(session, session_id, turn_seq)
        if _retry_permission(previous, pending):
            pending.claimed_by_replica = None
            pending.claimed_at = None
            session.add(pending)
        else:
            _finish_unknown_locked(session, row, pending, cause)
        session.commit()
        return session.exec(select(_unknown_outcome_exists(session_id))).one()


def persist_turn_from_pending_sync(
    session_id: int,
    turn_seq: int,
    prompt: str,
    turn: Turn,
    voice_summary: str,
    status: str,
    cli_session_id: str | None = None,
    model: str | None = None,
    claim_owner: str | None = None,
    dispatch_count: int | None = None,
) -> AgentTurn:
    """Persist the result of a queued message using a fresh database session."""
    with Session(get_engine()) as session:
        sess_row = _lock_session(session, session_id)
        if not sess_row:
            raise ValueError(f"Session {session_id} not found")
        _assert_sendable(session, session_id)
        pending = get_pending_message(session, session_id, turn_seq)
        if claim_owner is not None and (
            pending is None
            or pending.claimed_by_replica != claim_owner
            or (dispatch_count is not None and pending.dispatch_count != dispatch_count)
        ):
            raise PendingClaimLost("Turn completion no longer owns its dispatch")
        usage = {**turn.usage, "activities": turn.activities}
        if turn.workspace_recovery is not None:
            usage["workspace_recovery"] = turn.workspace_recovery
        diff_blob = None
        diff_truncated = False
        diff_base_sha = None
        if turn.diff is not None:
            try:
                diff_base_sha = turn.diff["base_sha"]
                diff_truncated = turn.diff["truncated"]
                encoded_diff = turn.diff.get("zlib_b64")
                if encoded_diff is not None:
                    diff_blob = base64.b64decode(encoded_diff, validate=True)
            except (KeyError, TypeError, ValueError, binascii.Error):
                diff_blob = None
                diff_truncated = False
                diff_base_sha = None
        artifact_path = None
        artifact_blob = None
        artifact_outcome = None
        if turn.artifact is not None:
            try:
                artifact_path = turn.artifact["path"]
                artifact_outcome = turn.artifact["outcome"]
                encoded_artifact = turn.artifact.get("content_b64")
                if encoded_artifact is not None:
                    artifact_blob = base64.b64decode(encoded_artifact, validate=True)
            except (KeyError, TypeError, ValueError, binascii.Error):
                artifact_path = None
                artifact_blob = None
                artifact_outcome = None
        existing_turn = get_turn(session, session_id, turn_seq)
        if (
            existing_turn is not None
            and existing_turn.terminal_reason in INTERRUPTED_TERMINAL_REASONS
        ):
            session.delete(existing_turn)
            session.flush()
        row = create_turn(
            session,
            session_id,
            turn_seq,
            prompt,
            voice_summary,
            turn.result,
            turn.terminal_reason,
            turn.stop_reason,
            turn.permission_denials,
            None,
            usage,
            turn.total_cost_usd,
            cli_session_id,
            model,
            diff_blob=diff_blob,
            diff_truncated=diff_truncated,
            diff_base_sha=diff_base_sha,
            artifact_path=artifact_path,
            artifact_blob=artifact_blob,
            artifact_outcome=artifact_outcome,
            commit=False,
        )
        if (
            turn.terminal_reason in CLEAN_TERMINAL_REASONS
            and sess_row.recovery_workspace_loss is not None
        ):
            sess_row.recovery_workspace_loss = None
            sess_row.recovery_completed_at = datetime.now(timezone.utc)
            session.add(sess_row)
        # The guest-reported CLI session_id is authoritative for this VM.
        if cli_session_id:
            sess_row.cli_session_id = cli_session_id
            session.add(sess_row)
        sess_row.status = status
        sess_row.voice_summary = voice_summary
        sess_row.last_turn_at = datetime.now(timezone.utc)
        session.add(sess_row)
        if pending is not None:
            session.delete(pending)
        session.commit()
        session.refresh(row)
        return row


def delete_pending_message_sync(session_id: int, turn_seq: int) -> None:
    """Delete a pending message after its turn has been persisted."""
    with Session(get_engine()) as session:
        row = get_pending_message(session, session_id, turn_seq)
        if row:
            session.delete(row)
            session.commit()


def mark_turn_error_sync(
    session_id: int, turn_seq: int, error_msg: str, claim_owner: str | None = None
) -> None:
    """Retain progress on a terminal delivery error without allowing replay."""
    with Session(get_engine()) as session:
        sess = _lock_session(session, session_id)
        row = get_pending_message(session, session_id, turn_seq)
        if sess is None or row is None:
            return
        if claim_owner is not None and row.claimed_by_replica != claim_owner:
            return
        existing = get_turn(session, session_id, turn_seq)
        if (
            existing is not None
            and existing.terminal_reason not in INTERRUPTED_TERMINAL_REASONS
        ):
            session.delete(row)
            session.commit()
            return
        if existing is not None:
            session.delete(existing)
            session.flush()
        error_summary = "Error: " + error_msg[:100]
        create_turn(
            session,
            session_id,
            turn_seq,
            row.message_text,
            error_summary,
            row.partial_text or f"Error executing turn: {error_msg}",
            terminal_reason="error",
            stop_reason=None,
            permission_denials=[],
            commit_sha=None,
            usage=_progress_usage(row, error_msg),
            cost_usd=None,
            model=row.model,
            commit=False,
        )
        sess.status = "warn"
        sess.voice_summary = error_summary
        sess.last_turn_at = datetime.now(timezone.utc)
        session.add(sess)
        session.delete(row)
        session.commit()


def mark_turn_interrupted_sync(
    session_id: int, turn_seq: int, claim_owner: str | None = None
) -> None:
    """Grant one retry for this exact, explicitly preempted dispatch attempt."""
    with Session(get_engine()) as session:
        sess = _lock_session(session, session_id)
        pending = get_pending_message(session, session_id, turn_seq)
        if sess is None or pending is None:
            return
        if claim_owner is not None and pending.claimed_by_replica != claim_owner:
            return
        existing = get_turn(session, session_id, turn_seq)
        if (
            existing is not None
            and existing.terminal_reason not in INTERRUPTED_TERMINAL_REASONS
        ):
            return
        usage = _progress_usage(pending, "brick_preempted")
        usage["retry_dispatch_count"] = pending.dispatch_count
        if existing is not None:
            usage["prior_interruption"] = {
                "result_text": existing.result_text,
                "usage_json": existing.usage_json,
            }
            session.delete(existing)
            session.flush()
        create_turn(
            session,
            session_id,
            turn_seq,
            pending.message_text,
            "Resuming after preemption",
            pending.partial_text
            or "The VM running this turn was preempted; the turn will be re-run.",
            terminal_reason="interrupted",
            stop_reason="brick_preempted",
            permission_denials=[],
            commit_sha=None,
            usage=usage,
            cost_usd=None,
            model=pending.model,
            commit=False,
        )
        sess.status = "recovering"
        sess.voice_summary = "Resuming after preemption"
        sess.last_turn_at = datetime.now(timezone.utc)
        session.add(sess)
        session.commit()


def reclaim_stale_claims_sync() -> int:
    """Dispose stale attempts without inferring that their invocations stopped."""
    now = datetime.now(timezone.utc)
    cutoff = now - RECLAIM_LEASE
    with Session(get_engine()) as session:
        candidates = session.exec(
            select(
                PendingMessage.session_id,
                PendingMessage.seq,
                PendingMessage.claimed_by_replica,
                PendingMessage.dispatch_count,
            ).where(
                PendingMessage.claimed_by_replica.isnot(None),
                PendingMessage.claimed_at.isnot(None),
                PendingMessage.claimed_at < cutoff,
            )
        ).all()
    reclaimed = 0
    for session_id, seq, owner, attempt in candidates:
        with Session(get_engine()) as session:
            row = _lock_session(session, session_id)
            pending = session.exec(
                select(PendingMessage).where(
                    PendingMessage.session_id == session_id,
                    PendingMessage.seq == seq,
                    PendingMessage.claimed_by_replica == owner,
                    PendingMessage.dispatch_count == attempt,
                    PendingMessage.claimed_at < cutoff,
                    _no_live_pending_claim(PendingMessage.session_id, now),
                )
            ).first()
            if row is None or pending is None:
                continue
            previous = get_turn(session, session_id, seq)
            if _retry_permission(previous, pending):
                pending.claimed_by_replica = None
                pending.claimed_at = None
                session.add(pending)
            else:
                _finish_unknown_locked(session, row, pending, "lease_expired")
            session.commit()
            reclaimed += 1
    return reclaimed


def refresh_claim_sync(session_id: int, turn_seq: int, replica_id: str) -> bool:
    """Refresh the claim on a pending message to prevent expiry during execution.

    Called periodically during turn execution to keep the claim fresh. If the
    claim has already been reclaimed by another replica (claimed_by_replica no
    longer matches replica_id), returns False so the executor knows to abort.

    Returns True if claim is still held, False if claim was stolen.
    """
    with Session(get_engine()) as session:
        _lock_session(session, session_id)
        # Match the owner in the UPDATE itself. A recovery transaction that
        # clears claimed_by_replica while this call is waiting for the row lock
        # therefore makes this return False instead of reviving the lease.
        result = session.execute(
            update(PendingMessage)
            .where(
                PendingMessage.session_id == session_id,
                PendingMessage.seq == turn_seq,
                PendingMessage.claimed_by_replica == replica_id,
            )
            .values(claimed_at=func.now())
        )
        session.commit()
        return result.rowcount == 1


def get_all_pending_messages_sync() -> list[PendingMessage]:
    """Fetch all pending messages using a fresh synchronous database session."""
    with Session(get_engine()) as session:
        return list(
            session.exec(
                select(PendingMessage)
                .join(AgentSession, AgentSession.id == PendingMessage.session_id)
                .where(
                    AgentSession.status.notin_({"awaiting_login", "failed"}),
                    ~_unknown_outcome_exists(AgentSession.id),
                    or_(
                        AgentSession.status != "recovering",
                        exists().where(
                            AgentTurn.session_id == PendingMessage.session_id,
                            AgentTurn.seq == PendingMessage.seq,
                            AgentTurn.terminal_reason.in_(INTERRUPTED_TERMINAL_REASONS),
                        ),
                    ),
                )
            ).all()
        )
