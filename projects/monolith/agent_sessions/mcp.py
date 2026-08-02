from __future__ import annotations

import asyncio
import json
import logging
import platform
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

import agent.api as agent_api
from agent_sessions import store, voice
from agent_sessions.models import AgentSession, AgentTurn
from agent_sessions.transport import (
    EmberSession,
    EmberSessionGone,
    EmberVmShimTransport,
    Turn,
)
from core.db import get_engine
from core.mcp_app import mcp
from framework import log_task_exception

_transport = EmberVmShimTransport()
_sweep_task: asyncio.Task | None = None
_REPLICA_ID = platform.node()
logger = logging.getLogger(__name__)


def _load_session(session_id: int) -> tuple[AgentSession | None, list[AgentTurn]]:
    with Session(get_engine()) as db_session:
        row = store.get_session(db_session, session_id)
        return row, store.get_turns(db_session, session_id) if row else []


def _ember_session(row: AgentSession) -> EmberSession | None:
    if row.ember_session_id and row.ember_session_token:
        return EmberSession(
            row.ember_session_id,
            row.ember_session_token,
            row.ember_session_expires_at,
        )
    return None


def _persist_ember_session(session_id: int, ember: EmberSession) -> None:
    with Session(get_engine()) as db_session:
        store.set_ember_session(
            db_session,
            session_id,
            ember.session_id,
            ember.session_token,
            ember.expires_at,
        )


def _persist_pending_message(session_id: int, message_text: str) -> int:
    with Session(get_engine()) as db_session:
        row = store.create_pending_message(db_session, session_id, message_text)
        assert row.seq is not None
        return row.seq


def _persist_session(
    local_session_id: str, workspace: str, branch: str
) -> AgentSession:
    with Session(get_engine()) as db_session:
        return store.create_session(db_session, local_session_id, workspace, branch)


def _turn_status(turn: Turn) -> str:
    # permission_denials is the signal for "agent blocked waiting on user";
    # stop_reason enum (end_turn, max_tokens, etc.) never indicates user input needed
    if turn.permission_denials:
        return "needs_input"
    if turn.is_error or turn.terminal_reason != "completed":
        return "warn"
    return "completed"


async def _notify_terminal(turn: Turn, summary: str, status: str) -> None:
    if turn.permission_denials or status == "needs_input":
        level = "warn"  # Needs user action
    elif status == "completed":
        level = "info"
    else:
        level = "warn"
    await agent_api.notify(summary, level=level)


def _get_pending_message_sync(session_id: int, turn_seq: int):
    return store.get_pending_message_sync(session_id, turn_seq)


def _claim_pending_message_sync(session_id: int) -> int | None:
    """Claim this session's next queued message, returning its seq.

    The database picks the message, not the caller: it claims the lowest
    unclaimed seq for the session in one atomic statement. That is what makes
    ordering hold across replicas, so nothing outside this call may choose
    which message runs next.
    """
    return store.claim_pending_message_for_session_sync(session_id, _REPLICA_ID)


def _release_pending_message_claim_sync(session_id: int, turn_seq: int) -> None:
    store.release_pending_message_claim_sync(session_id, turn_seq, _REPLICA_ID)


def _persist_turn_from_pending_sync(
    session_id: int,
    turn_seq: int,
    prompt: str,
    turn: Turn,
    voice_summary: str,
    status: str,
    cli_session_id: str | None = None,
) -> AgentTurn:
    return store.persist_turn_from_pending_sync(
        session_id, turn_seq, prompt, turn, voice_summary, status, cli_session_id
    )


def _delete_pending_message_sync(session_id: int, turn_seq: int) -> None:
    store.delete_pending_message_sync(session_id, turn_seq)


def _mark_turn_error_sync(session_id: int, turn_seq: int, error_msg: str) -> None:
    store.mark_turn_error_sync(session_id, turn_seq, error_msg)


def _clear_ember_session_sync(session_id: int) -> None:
    with Session(get_engine()) as db_session:
        store.clear_ember_session(db_session, session_id)


def _get_all_pending_messages_sync():
    return store.get_all_pending_messages_sync()


def _reclaim_stale_claims_sync():
    """Reclaim messages whose claims have expired (replica crashed or hung)."""
    return store.reclaim_stale_claims_sync()


def _refresh_claim_sync(session_id: int, turn_seq: int, replica_id: str) -> bool:
    """Refresh heartbeat for an active claim. Returns True if claim still held."""
    return store.refresh_claim_sync(session_id, turn_seq, replica_id)


_inflight_tasks: set[asyncio.Task] = set()


def _schedule_next_message(session_id: int) -> None:
    """Run this session's next queued message in the background.

    The task is retained in a module-level set until it finishes. Without a
    strong reference the event loop only holds a weak one, so a task can be
    garbage collected mid-flight; and without the done-callback any exception
    escaping the executor is discarded silently. This is the same invariant
    app/main_summary_test.py asserts for the leader-elected singletons.
    """
    task = asyncio.create_task(_execute_pending_message(session_id))
    _inflight_tasks.add(task)
    task.add_done_callback(_inflight_tasks.discard)
    task.add_done_callback(log_task_exception)


async def _execute_pending_message(session_id: int) -> None:
    """Process this session's next queued message durably.

    Ordering comes from the claim itself. The database looks at the lowest
    OUTSTANDING seq for the session and claims it only if it is still unclaimed,
    so a later message cannot start while an earlier one is running. Pending
    rows are deleted when their turn completes, so anything still present is
    unfinished. Taking the lowest UNCLAIMED seq instead would order assignment
    but not execution: with seq 1 running, seq 2 would be the lowest unclaimed
    and would start alongside it.

    There is deliberately no in-process lock. One could not provide this
    guarantee anyway, since it says nothing about what another replica is doing.

    Once claimed, a background task refreshes the claim every 10 seconds. If a
    refresh reports the claim is no longer ours, another replica has reclaimed
    it and this turn aborts rather than writing a duplicate. That keeps a
    long-running turn (which may take many minutes) safe from reclaim while
    still recovering from a crashed replica within one lease interval
    (30s, three missed refreshes).
    """

    claimed_seq = await asyncio.to_thread(_claim_pending_message_sync, session_id)
    if claimed_seq is None:
        return

    # Set once the claim is lost to another replica; checked before starting and
    # again after deliver, so a stolen claim never persists a duplicate turn.
    claim_stolen = False
    refresh_task = None

    async def _refresh_heartbeat() -> None:
        """Keep the claim alive while the turn runs."""
        nonlocal claim_stolen
        while not claim_stolen:
            try:
                await asyncio.sleep(10)  # one third of the 30s lease
                still_held = await asyncio.to_thread(
                    _refresh_claim_sync, session_id, claimed_seq, _REPLICA_ID
                )
                if not still_held:
                    claim_stolen = True
                    logger.warning(
                        "Claim for turn %s in session %s was reclaimed, aborting execution",
                        claimed_seq,
                        session_id,
                    )
                    break
            except Exception:
                # A transient refresh failure must not kill an otherwise healthy
                # turn; the lease tolerates two missed beats.
                logger.exception(
                    "Failed to refresh claim for turn %s in session %s",
                    claimed_seq,
                    session_id,
                )

    async def _do_execute() -> None:
        if claim_stolen:
            return

        row = await asyncio.to_thread(
            _get_pending_message_sync, session_id, claimed_seq
        )
        if not row:
            return
        # Load session to get workspace and stored session_id for resumption
        session_row, _ = await asyncio.to_thread(_load_session, session_id)
        if not session_row:
            await asyncio.to_thread(
                _mark_turn_error_sync, session_id, claimed_seq, "Session not found"
            )
            return
        try:
            # Reuse the session_id from the database (from first turn), or None for new sessions
            cli_session_id = session_row.cli_session_id
            existing_ember = _ember_session(session_row)
            turn, ember = await _transport.deliver(
                existing_ember,
                cli_session_id,
                row.message_text,
            )
            # Check if claim was stolen while deliver was running
            if claim_stolen:
                logger.warning(
                    "Claim was stolen during deliver for turn %s in session %s, not persisting result",
                    claimed_seq,
                    session_id,
                )
                return
            if ember != existing_ember:
                await asyncio.to_thread(_persist_ember_session, session_id, ember)
        except EmberSessionGone as exc:
            # Session confirmed dead by CP; clear the binding and CLI id together.
            await asyncio.to_thread(_clear_ember_session_sync, session_id)
            await asyncio.to_thread(
                _mark_turn_error_sync, session_id, claimed_seq, str(exc)
            )
            return
        except Exception as exc:
            # Terminal failure: row is deleted by mark_turn_error_sync (noqa: BLE001)
            await asyncio.to_thread(
                _mark_turn_error_sync, session_id, claimed_seq, str(exc)
            )
            return

        summary = voice.extract_voice_summary(turn.result)
        status = _turn_status(turn)
        # Store the CLI's session_id from the turn for resumption on next deliver
        try:
            await asyncio.to_thread(
                _persist_turn_from_pending_sync,
                session_id,
                claimed_seq,
                row.message_text,
                turn,
                summary,
                status,
                turn.session_id,  # Store for resumption
            )
        except IntegrityError as e:
            # Turn already exists (duplicate seq), likely from a retry of a completed turn.
            # Delete the pending row to prevent infinite retry.
            logger.warning(
                "Turn %s for session %s already exists (duplicate seq), discarding retry",
                claimed_seq,
                session_id,
            )
            await asyncio.to_thread(
                store.delete_pending_message_sync, session_id, claimed_seq
            )
            return
        except Exception:
            logger.exception(
                "Failed to persist queued turn %s for session %s",
                claimed_seq,
                session_id,
            )
            return
        await asyncio.to_thread(_delete_pending_message_sync, session_id, claimed_seq)
        await _notify_terminal(turn, summary, status)
        # This session's next message could not be claimed while this one was
        # outstanding, so nudge the queue now that it is not. Without this the
        # follow-up waits for the sweep, which is correct but adds its interval
        # to every turn after the first. The nudge is safe to lose: the sweep
        # remains the backstop.
        _schedule_next_message(session_id)

    try:
        # Start the heartbeat refresh task
        refresh_task = asyncio.create_task(_refresh_heartbeat())
        await _do_execute()
    finally:
        # Cancel the refresh task
        if refresh_task:
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass
        await asyncio.to_thread(
            _release_pending_message_claim_sync, session_id, claimed_seq
        )


async def _sweep_orphaned_pending_messages() -> None:
    """Pick up pending messages left behind by a crash or restart.

    The sweep does two things:
    1. Reclaim any claims that have expired (replica crashed), making them
       available for re-execution by the current leader.
    2. Execute any pending messages that are not yet claimed.
    """
    while True:
        await asyncio.sleep(5)
        # Reclaim stale claims from crashed replicas
        reclaimed = await asyncio.to_thread(_reclaim_stale_claims_sync)
        if reclaimed > 0:
            logger.info("Reclaimed %d stale claims from crashed replicas", reclaimed)
        # Execute all unclaimed messages
        rows = await asyncio.to_thread(_get_all_pending_messages_sync)
        for row in rows:
            if row.claimed_by_replica is None:
                _schedule_next_message(row.session_id)


def start_pending_message_sweep() -> list[asyncio.Task]:
    """Start the leader-owned orphan sweep and return its tracked task."""
    global _sweep_task
    if _sweep_task is None or _sweep_task.done():
        _sweep_task = asyncio.create_task(_sweep_orphaned_pending_messages())
        _sweep_task.add_done_callback(log_task_exception)
    return [_sweep_task]


def _decode_json(value: str | None, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _activities(turn: AgentTurn) -> list[dict]:
    payload = _decode_json(turn.usage_json, {})
    return payload.get("activities", []) if isinstance(payload, dict) else []


def _activity_values(turns: list[AgentTurn]) -> tuple[list[str], list[str]]:
    files: list[str] = []
    commands: list[str] = []
    for turn in turns:
        for activity in _activities(turn):
            if activity.get("file_path") and activity["file_path"] not in files:
                files.append(activity["file_path"])
            if activity.get("command") and activity["command"] not in commands:
                commands.append(activity["command"])
    return files, commands


@mcp.tool
async def monolith_agent_session_start(prompt: str) -> dict:
    """Start a voice-drivable Claude Code session and queue its first turn."""
    local_session_id = str(uuid4())
    workspace = "<guest>"  # Workspace is in the guest, not the pod
    row = await asyncio.to_thread(_persist_session, local_session_id, workspace, "main")
    turn = await asyncio.to_thread(_persist_pending_message, row.id, prompt)
    _schedule_next_message(row.id)
    return {"accepted": True, "session_id": row.id, "turn": turn}


@mcp.tool
async def monolith_agent_session_send(session_id: int, message: str) -> dict:
    """Enqueue a message for a session, returning once accepted rather than once complete."""
    turn = await asyncio.to_thread(_persist_pending_message, session_id, message)
    _schedule_next_message(session_id)
    return {"accepted": True, "session_id": session_id, "turn": turn}


@mcp.tool
async def monolith_agent_session_status(session_id: int) -> dict:
    """Return session status, voice summary, activity aggregates, and cost."""
    row, turns = await asyncio.to_thread(_load_session, session_id)
    if row is None:
        raise ValueError(f"Unknown agent session {session_id}")
    files, commands = _activity_values(turns)
    last = turns[-1] if turns else None
    denials = _decode_json(last.permission_denials, []) if last else []
    return {
        "status": row.status,
        "voice": row.voice_summary,
        "files_touched": files,
        "commands_run": commands,
        "needs_answer": bool(denials) or row.status == "needs_input",
        "cost_usd": sum(turn.cost_usd or 0 for turn in turns),
    }


def _select_turn(turns: list[AgentTurn], turn: int | None) -> AgentTurn:
    if not turns:
        raise ValueError("Agent session has no turns")
    if turn is None:
        return turns[-1]
    for row in turns:
        if row.seq == turn:
            return row
    raise ValueError(f"Unknown turn {turn}")


@mcp.tool
async def monolith_agent_detail(session_id: int, turn: int | None = None) -> dict:
    """Return the verbatim result and tool activities for one session turn."""
    _, turns = await asyncio.to_thread(_load_session, session_id)
    selected = _select_turn(turns, turn)
    return {"result_text": selected.result_text, "activities": _activities(selected)}
