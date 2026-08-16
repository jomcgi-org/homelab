from __future__ import annotations

import base64
import binascii
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, text, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

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
    row = session.get(AgentSession, session_id)
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
        usage_json=json.dumps(usage or {}),
        cost_usd=cost_usd,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def update_session_status(
    session: Session, session_id: int, status: str, voice_summary: str | None = None
) -> AgentSession:
    row = session.get(AgentSession, session_id)
    if row is None:
        raise ValueError(f"Unknown agent session {session_id}")
    row.status = status
    row.last_turn_at = datetime.now(timezone.utc)
    if voice_summary is not None:
        row.voice_summary = voice_summary
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


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
        "ORDER BY rank DESC, t.created_at DESC LIMIT :limit"
        ") AS ranked"
    )
    result = session.exec(sql, params={"q": query_text, "limit": limit})
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
    if session.get(AgentSession, session_id) is None:
        raise ValueError(f"Unknown agent session {session_id}")

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
            # Continue to next attempt


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
    """Write guest progress to the active pending message.

    Returns ``ok`` when a row was updated, ``unknown_token`` when the token
    is not in ``agent_sessions``, and ``no_row`` when no pending row exists.
    """
    with Session(get_engine()) as session:
        session_row = session.exec(
            select(AgentSession.id).where(AgentSession.progress_token == progress_token)
        ).first()
        if session_row is None:
            return "unknown_token"
        session_id = session_row

    with Session(get_engine()) as session:
        update_kwargs = {"partial_text": partial_text}
        if activities is not None:
            update_kwargs["partial_activities"] = json.dumps(activities)
        stmt = (
            update(PendingMessage)
            .where(
                PendingMessage.session_id == session_id,
                PendingMessage.claimed_by_replica.isnot(None),
            )
            .values(**update_kwargs)
        )
        result = session.execute(stmt)
        session.commit()
        if result.rowcount > 0:
            return "ok"

        lowest_seq = session.exec(
            select(PendingMessage.seq)
            .where(PendingMessage.session_id == session_id)
            .order_by(PendingMessage.seq)
        ).first()
        if lowest_seq is None:
            return "no_row"
        stmt = (
            update(PendingMessage)
            .where(
                PendingMessage.session_id == session_id,
                PendingMessage.seq == lowest_seq,
            )
            .values(**update_kwargs)
        )
        result = session.execute(stmt)
        session.commit()
        return "ok" if result.rowcount > 0 else "no_row"


def _persist_progress_token_sync(session_id: int, progress_token: str) -> None:
    """Mint and persist a progress token for a pre-migration session."""
    with Session(get_engine()) as session:
        row = session.get(AgentSession, session_id)
        if row is not None and row.progress_token is None:
            row.progress_token = progress_token
            session.add(row)
            session.commit()


def claim_pending_message_for_session_sync(
    session_id: int, replica_id: str
) -> int | None:
    """Atomically claim the lowest unclaimed seq for a session.

    Enforces FIFO ordering across replicas by always claiming the lowest
    unclaimed seq. This is a single atomic operation, so ordering is
    guaranteed at the database level and holds across all replicas.

    Returns the seq of the claimed message, or None if no unclaimed messages.
    """
    with Session(get_engine()) as session:
        # The lowest OUTSTANDING seq, claimed or not. Pending rows are deleted
        # once their turn completes, so anything still here is unfinished.
        #
        # Taking the minimum over unclaimed rows only would order assignment but
        # not execution: with seq 1 claimed and running, seq 2 would be the
        # lowest unclaimed and a second executor would claim it and run
        # concurrently. Serialising within a session means refusing to start a
        # later message while an earlier one is still outstanding.
        lowest_seq_result = session.execute(
            select(func.min(PendingMessage.seq)).where(
                PendingMessage.session_id == session_id,
            )
        ).scalar()

        if lowest_seq_result is None:
            return None

        # Claim it atomically
        result = session.execute(
            update(PendingMessage)
            .where(
                PendingMessage.session_id == session_id,
                PendingMessage.seq == lowest_seq_result,
                PendingMessage.claimed_by_replica.is_(None),
            )
            .values(claimed_by_replica=replica_id, claimed_at=func.now())
        )
        session.commit()

        # Return the seq if we successfully claimed it, None otherwise
        return lowest_seq_result if result.rowcount == 1 else None


def release_pending_message_claim_sync(
    session_id: int, turn_seq: int, replica_id: str
) -> None:
    """Release this replica's claim after execution completes."""
    with Session(get_engine()) as session:
        session.execute(
            update(PendingMessage)
            .where(
                PendingMessage.session_id == session_id,
                PendingMessage.seq == turn_seq,
                PendingMessage.claimed_by_replica == replica_id,
            )
            .values(claimed_by_replica=None, claimed_at=None)
        )
        session.commit()


def persist_turn_from_pending_sync(
    session_id: int,
    turn_seq: int,
    prompt: str,
    turn: Turn,
    voice_summary: str,
    status: str,
    cli_session_id: str | None = None,
    model: str | None = None,
) -> AgentTurn:
    """Persist the result of a queued message using a fresh database session."""
    with Session(get_engine()) as session:
        sess_row = get_session(session, session_id)
        if not sess_row:
            raise ValueError(f"Session {session_id} not found")
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
                if not diff_truncated:
                    diff_blob = base64.b64decode(turn.diff["zlib_b64"], validate=True)
            except (KeyError, TypeError, ValueError, binascii.Error):
                diff_blob = None
                diff_truncated = False
                diff_base_sha = None
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
        )
        # The guest-reported CLI session_id is authoritative for this VM.
        if cli_session_id:
            sess_row.cli_session_id = cli_session_id
            session.add(sess_row)
            session.commit()
        update_session_status(session, session_id, status, voice_summary)
        return row


def delete_pending_message_sync(session_id: int, turn_seq: int) -> None:
    """Delete a pending message after its turn has been persisted."""
    with Session(get_engine()) as session:
        row = get_pending_message(session, session_id, turn_seq)
        if row:
            session.delete(row)
            session.commit()


def mark_turn_error_sync(session_id: int, turn_seq: int, error_msg: str) -> None:
    """Persist an error turn and delete the pending row to stop infinite retries.

    Semantics: A failed turn is terminal - the error is recorded durably in
    agent_turns as a failed attempt, and the pending row is deleted to prevent
    the sweep from re-dispatching it every 5 seconds. The caller can query
    session status to see the failure.
    """
    with Session(get_engine()) as session:
        row = get_pending_message(session, session_id, turn_seq)
        if row is None or get_turn(session, session_id, turn_seq) is not None:
            return
        error_summary = "Error: " + error_msg[:100]
        create_turn(
            session,
            session_id,
            turn_seq,
            row.message_text,
            error_summary,
            f"Error executing turn: {error_msg}",
            terminal_reason="error",
            stop_reason=None,
            permission_denials=[],
            commit_sha=None,
            usage={},
            cost_usd=0.0,
            model=row.model,
        )
        update_session_status(session, session_id, "warn", error_summary)
        # Delete the pending row to stop infinite retries
        session.delete(row)
        session.commit()


def reclaim_stale_claims_sync(lease_interval_seconds: int = 30) -> int:
    """Reclaim pending messages whose claims have expired due to replica crash.

    A claim is considered stale if claimed_by_replica is not null and the
    claimed_at timestamp is older than lease_interval_seconds. A healthy
    replica refreshes claimed_at every 10 seconds during turn execution, so
    a claim that is not refreshed will be reclaimed within one lease interval.

    The lease interval is set as a multiple of the refresh interval
    (default 30s = 3x10s refresh), trading slow recovery for correctness:
    a crashed replica's claims are reclaimed within one lease interval, but
    an actively executing turn that refreshes its claim will never be
    double-executed (even if the turn takes many minutes).

    ASSUMPTION, and the one thing to check if turns are ever double-executed:
    claimed_at is written by the DATABASE (func.now()) but the cutoff below is
    computed from THIS POD's clock, because SQLAlchemy will not render timedelta
    arithmetic portably across SQLite and Postgres. So the lease is only sound
    while pod and database clocks agree to well within the interval.

    The two directions of skew are not equally bad. A pod running behind the
    database sees claims as fresher than they are, which only delays recovery.
    A pod running ahead sees them as staler and can reclaim a turn that is still
    executing, which is the double-execution the lease exists to prevent. NTP
    keeps this far inside 30s in practice, but the dangerous direction fails
    silently, so widen the lease rather than narrow it if this is ever in doubt.

    Returns the count of reclaimed messages.
    """
    with Session(get_engine()) as session:
        # Compute cutoff in Python, not as a SQL expression. SQLAlchemy cannot
        # reliably render timedelta subtraction across SQLite and Postgres dialects.
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=lease_interval_seconds)
        result = session.execute(
            update(PendingMessage)
            .where(
                PendingMessage.claimed_by_replica.isnot(None),
                PendingMessage.claimed_at.isnot(None),
                PendingMessage.claimed_at < cutoff,
            )
            .values(claimed_by_replica=None, claimed_at=None)
        )
        session.commit()
    return result.rowcount


def refresh_claim_sync(session_id: int, turn_seq: int, replica_id: str) -> bool:
    """Refresh the claim on a pending message to prevent expiry during execution.

    Called periodically during turn execution to keep the claim fresh. If the
    claim has already been reclaimed by another replica (claimed_by_replica no
    longer matches replica_id), returns False so the executor knows to abort.

    Returns True if claim is still held, False if claim was stolen.
    """
    with Session(get_engine()) as session:
        row = get_pending_message(session, session_id, turn_seq)
        if not row:
            return False
        if row.claimed_by_replica != replica_id:
            return False
        # Claim is still ours; refresh the timestamp using SQL so no Python
        # datetime crosses the boundary, avoiding SQLite/Postgres tz handling issues
        session.execute(
            update(PendingMessage)
            .where(
                PendingMessage.session_id == session_id,
                PendingMessage.seq == turn_seq,
            )
            .values(claimed_at=func.now())
        )
        session.commit()
        return True


def get_all_pending_messages_sync() -> list[PendingMessage]:
    """Fetch all pending messages using a fresh synchronous database session."""
    with Session(get_engine()) as session:
        return list(session.exec(select(PendingMessage)).all())
