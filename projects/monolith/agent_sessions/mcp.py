from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import platform
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

import httpx

import agent.api as agent_api
from agent_sessions import store, voice, voice_ui
from agent_sessions import model_family, normalize_model
from agent_sessions.constants import (
    CLEAN_TERMINAL_REASONS,
    DRAINER_NODE_KEY,
    INTERRUPTED_TERMINAL_REASONS,
)
from agent_sessions.rationale import rationale_trailer_instruction
from agent_sessions.models import AgentSession, AgentTurn
from agent_sessions.transport import (
    EmberBrickGone,
    EmberSession,
    EmberSessionGone,
    EmberVmShimTransport,
    Turn,
)
from core.db import get_engine
from core.github import GITHUB_REPO
from core.mcp_app import mcp
from faas.embervm_client import EmberVMTransportError
from framework import log_task_exception
from goosecracker.api import REPO_CATALOG
from knowledge.api import attach_recall
from agent_sessions.rationale import parse_rationale
from auth.api import Authority, current_principal

# Voice and MCP sessions hydrate this repo unless the caller names another. The
# /agents console makes the choice explicit in a dropdown; there is no dropdown
# here, and a session with no checkout cannot do the work these calls ask for
# (read the code, open a PR), so an empty workspace is the wrong default for
# this surface even though it is the cheaper one.
#
# Cost is real and per SESSION, not per repo: volumes are per lineage, so every
# new session clones again. Issue #4473 moves the clone back to a node-local
# mirror over http, which is what makes this default cheap rather than merely
# convenient.
DEFAULT_AGENT_REPO = GITHUB_REPO

_transport = EmberVmShimTransport()
_sweep_task: asyncio.Task | None = None
_REPLICA_ID = platform.node()
logger = logging.getLogger(__name__)

# A first turn normally claims its pending row within seconds. Three minutes
# leaves ample room for broker and scheduler jitter while bounding how long a
# session can remain behind a workflow owned by a dead application version.
ZOMBIE_SESSION_THRESHOLD_SECONDS = 180
# A claim heartbeat updates every 10 seconds. If the same dispatch is still
# live after 600 seconds and its bound guest was never invoked, the executor is
# hung before delivery and recovery may steal the claim.
HUNG_CLAIM_THRESHOLD_SECONDS = 600
# A negative control-plane oracle result suppresses repeated full listings from
# both sends and the five-second sweep. This cache is advisory and process-local.
NEGATIVE_ORACLE_TTL_SECONDS = 120
_negative_oracle_verdicts: dict[int, float] = {}


class _ClaimStolen(RuntimeError):
    """Abort a delivery callback after its pending-message lease is stolen."""


def _negative_oracle_verdict_is_fresh(session_id: int) -> bool:
    expires_at = _negative_oracle_verdicts.get(session_id)
    if expires_at is None:
        return False
    if expires_at <= time.monotonic():
        _negative_oracle_verdicts.pop(session_id, None)
        return False
    return True


def _memoize_negative_oracle_verdict(session_id: int) -> None:
    _negative_oracle_verdicts[session_id] = (
        time.monotonic() + NEGATIVE_ORACLE_TTL_SECONDS
    )


def _clear_negative_oracle_verdict(session_id: int) -> None:
    _negative_oracle_verdicts.pop(session_id, None)


# Messages queued by the /agents UI, so their terminal Discord post can be
# suppressed: someone watching the UI is already looking at the result and does
# not need it echoed to a channel. Only MCP-originated turns notify.
#
# Deliberately process-local rather than a column on pending_messages. That
# makes it BEST EFFORT: a message the sweep hands to another replica, or one
# outliving a restart, loses its mark and notifies anyway. That direction is
# fail-open, which for a notification is the benign one (a stray post, never a
# missed one). Move the flag onto the row if the strays become annoying.
#
# Bounded because an entry is only consumed when THIS replica claims the
# message; eviction of the oldest is the same benign fail-open.
_UI_ORIGINATED_CAP = 1024
_ui_originated: collections.OrderedDict[tuple[int, int], bool] = (
    collections.OrderedDict()
)


def _mark_ui_originated(session_id: int, seq: int) -> None:
    """Flag a queued message as UI-originated so its turn skips the notify."""
    _ui_originated[(session_id, seq)] = True
    while len(_ui_originated) > _UI_ORIGINATED_CAP:
        _ui_originated.popitem(last=False)


def _consume_ui_originated(session_id: int, seq: int) -> bool:
    """Read and clear the UI mark for a message.

    Called once, at claim time rather than at notify time, because the turn has
    several early-return paths and this way the entry clears on all of them
    instead of leaking on the ones that never reach the notify.
    """
    return _ui_originated.pop((session_id, seq), False)


def _load_session(session_id: int) -> tuple[AgentSession | None, list[AgentTurn]]:
    with Session(get_engine()) as db_session:
        row = store.get_session(db_session, session_id)
        return row, store.get_turns(db_session, session_id) if row else []


def _load_session_row(session_id: int) -> AgentSession | None:
    with Session(get_engine()) as db_session:
        return store.get_session(db_session, session_id)


def _set_session_status(session_id: int, status: str) -> None:
    with Session(get_engine()) as db_session:
        store.update_session_status(db_session, session_id, status)
    if status in {"completed", "failed"}:
        _clear_negative_oracle_verdict(session_id)


def _activate_session_after_enqueue(session_id: int) -> bool:
    with Session(get_engine()) as db_session:
        return store.activate_session_after_enqueue(db_session, session_id)


def _ember_session(row: AgentSession) -> EmberSession | None:
    if row.ember_session_id and row.ember_session_token:
        return EmberSession(
            row.ember_session_id,
            row.ember_session_token,
            row.ember_session_expires_at,
            row.ember_lineage_id,
            # restored is transient (whether THIS turn's create actually
            # recovered the workspace, #4306 slice 4): a session loaded
            # back out of the row is never "just restored", so this is
            # always False here regardless of how it was created.
            False,
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
            ember.lineage_id,
            is_restored=ember.restored,
        )


def _persist_ember_session_and_cli(
    session_id: int, ember: EmberSession, cli_session_id: str | None
) -> None:
    with Session(get_engine()) as db_session:
        store.set_ember_session(
            db_session,
            session_id,
            ember.session_id,
            ember.session_token,
            ember.expires_at,
            ember.lineage_id,
            cli_session_id=cli_session_id,
            is_restored=ember.restored,
        )


def _replace_ember_session_after_preemption(
    session_id: int, ember: EmberSession
) -> None:
    with Session(get_engine()) as db_session:
        store.replace_ember_session_after_preemption(
            db_session,
            session_id,
            ember.session_id,
            ember.session_token,
            ember.expires_at,
            ember.lineage_id,
        )


def _clear_ember_bindings_for(ember_id: str) -> list[int]:
    with Session(get_engine()) as db_session:
        return store.clear_ember_bindings_by_ember_id(db_session, ember_id)


def _persist_pending_message(
    session_id: int, message_text: str, model: str | None
) -> int:
    with Session(get_engine()) as db_session:
        row = store.create_pending_message(db_session, session_id, message_text, model)
        assert row.seq is not None
        return row.seq


def _append_rationale_trailer(
    system_prompt: str | None, repo: str | None
) -> str | None:
    """Add walkthrough rationale guidance when the session has a repository."""
    if repo is None:
        return system_prompt
    trailer = rationale_trailer_instruction()
    if system_prompt is None:
        return trailer
    return f"{system_prompt.rstrip()}\n\n{trailer}"


def _persist_session(
    local_session_id: str,
    workspace: str,
    branch: str,
    model: str | None,
    repo: str | None = None,
    *,
    discord_thread: str | None = None,
    system_prompt: str | None = None,
    prompt: str | None = None,
    reasoning: bool = False,
    workflow_id: str | None = None,
    triggered_by: str | None = None,
    node_key: str | None = None,
    node_attempt: int | None = None,
) -> AgentSession:
    system_prompt = attach_recall(system_prompt, prompt, node_key=node_key)
    with Session(get_engine()) as db_session:
        return store.create_session(
            db_session,
            local_session_id,
            workspace,
            branch,
            model,
            repo,
            discord_thread=discord_thread,
            system_prompt=system_prompt,
            reasoning=reasoning,
            workflow_id=workflow_id,
            triggered_by=triggered_by,
            node_key=node_key,
            node_attempt=node_attempt,
        )


def _turn_status(turn: Turn) -> str:
    if turn.terminal_reason in INTERRUPTED_TERMINAL_REASONS:
        return "recovering"
    # permission_denials is the signal for "agent blocked waiting on user";
    # stop_reason enum (end_turn, max_tokens, etc.) never indicates user input needed
    if turn.permission_denials:
        return "needs_input"
    if turn.is_error or turn.terminal_reason not in CLEAN_TERMINAL_REASONS:
        return "warn"
    return "completed"


# Discord's hard per-message limit is 2000; leave room for a status prefix and
# the outbox's own formatting.
_DISCORD_CHUNK = 1800
# Cap on how many messages one turn may post into a thread. A long agent turn
# can run to tens of KB, and pasting all of it would bury the thread.
_MAX_THREAD_CHUNKS = 4


def _chunk_for_discord(text: str) -> list[str]:
    """Split a turn result into Discord-sized messages, preferring line breaks.

    Bounded by _MAX_THREAD_CHUNKS with an explicit truncation marker, so a huge
    turn is visibly cut rather than silently losing its tail.
    """
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    while text and len(chunks) < _MAX_THREAD_CHUNKS:
        if len(text) <= _DISCORD_CHUNK:
            chunks.append(text)
            text = ""
            break
        split = text.rfind("\n", 0, _DISCORD_CHUNK)
        if split <= 0:
            split = _DISCORD_CHUNK
        chunks.append(text[:split].rstrip())
        text = text[split:].lstrip()
    if text:
        chunks[-1] = chunks[-1][: _DISCORD_CHUNK - 40].rstrip() + "\n\n... (truncated)"
    return chunks


async def _notify_terminal(
    turn: Turn, summary: str, status: str, row: AgentSession | None = None
) -> None:
    # The drainer owns its failure notification and suppresses successful jobs.
    # Skipping this generic session post keeps each failed job to one alert.
    if row is not None and row.node_key == DRAINER_NODE_KEY:
        return
    if turn.permission_denials or status == "needs_input":
        level = "warn"  # Needs user action
    elif status == "completed":
        level = "info"
    else:
        level = "warn"
    thread = row.discord_thread if row is not None else None
    if thread:
        # A thread is a reading surface, so post the VERBATIM result. The voice
        # summary is the first sentence capped at 200 chars, which is right for
        # the voice lane and useless here: it turned a multi-paragraph answer
        # into "The workspace is clean." with the rest silently dropped.
        body = turn.result or summary
        chunks = _chunk_for_discord(body) or [summary or "(no output)"]
        if status == "needs_input":
            chunks[0] = f"**Needs input**\n{chunks[0]}"
        elif status != "completed":
            chunks[0] = f"**Turn ended: {status}**\n{chunks[0]}"
        for chunk in chunks:
            await agent_api.notify(chunk, level=level, channel=thread)
        return

    # No thread bound (MCP or /agents UI): only turns selected by the configured
    # policy send their short voice summary to Discord. Unset channels still
    # fall back to notify()'s default channel.
    notify_policy = agent_api.agent_sessions_channel_notify()
    if notify_policy == "none" or (
        notify_policy == "needs-input" and status != "needs_input"
    ):
        return
    if row is not None and status == "needs_input":
        summary = f"Needs input: {summary}"
    elif row is not None and status == "warn":
        summary = f"Warning: {summary}"
    await agent_api.notify(
        summary[:2000],
        level=level,
        channel=agent_api.agent_sessions_channel_id(),
    )


def _get_pending_message_sync(session_id: int, turn_seq: int):
    return store.get_pending_message_sync(session_id, turn_seq)


def _claim_pending_message_sync(
    session_id: int, claim_owner: str | None = None
) -> int | None:
    """Claim this session's next queued message, returning its seq.

    The database picks the message, not the caller: it claims the lowest
    unclaimed seq for the session in one atomic statement. That is what makes
    ordering hold across replicas, so nothing outside this call may choose
    which message runs next.
    """
    return store.claim_pending_message_for_session_sync(
        session_id, claim_owner or _REPLICA_ID
    )


def _release_pending_message_claim_sync(
    session_id: int, turn_seq: int, claim_owner: str | None = None
) -> None:
    store.release_pending_message_claim_sync(
        session_id, turn_seq, claim_owner or _REPLICA_ID
    )


def _persist_turn_from_pending_sync(
    session_id: int,
    turn_seq: int,
    prompt: str,
    turn: Turn,
    voice_summary: str,
    status: str,
    cli_session_id: str | None = None,
    model: str | None = None,
) -> AgentTurn:
    return store.persist_turn_from_pending_sync(
        session_id,
        turn_seq,
        prompt,
        turn,
        voice_summary,
        status,
        cli_session_id,
        model,
    )


def _delete_pending_message_sync(session_id: int, turn_seq: int) -> None:
    store.delete_pending_message_sync(session_id, turn_seq)


def _mark_turn_error_sync(session_id: int, turn_seq: int, error_msg: str) -> None:
    store.mark_turn_error_sync(session_id, turn_seq, error_msg)


def _mark_turn_interrupted_sync(session_id: int, turn_seq: int) -> None:
    store.mark_turn_interrupted_sync(session_id, turn_seq)


def _clear_ember_session_sync(session_id: int) -> None:
    with Session(get_engine()) as db_session:
        store.clear_ember_session(db_session, session_id)


def _get_all_pending_messages_sync():
    return store.get_all_pending_messages_sync()


def _find_zombie_session_ids_sync(
    cutoff: datetime, now: datetime, limit: int = 5
) -> list[int]:
    with Session(get_engine()) as db_session:
        return store.find_zombie_session_ids(db_session, cutoff, now, limit)


def _find_hung_claim_session_ids_sync(
    cutoff: datetime, now: datetime, limit: int = 5
) -> list[int]:
    with Session(get_engine()) as db_session:
        return store.find_hung_claim_session_ids(db_session, cutoff, now, limit)


def _get_hung_claim_binding_sync(
    session_id: int, cutoff: datetime, now: datetime
) -> str | None:
    with Session(get_engine()) as db_session:
        return store.get_hung_claim_binding(db_session, session_id, cutoff, now)


def _claim_zombie_session_recovery_sync(
    session_id: int, cutoff: datetime, now: datetime
) -> dict | None:
    with Session(get_engine()) as db_session:
        return store.claim_zombie_session_recovery(db_session, session_id, cutoff, now)


def _claim_hung_zombie_session_recovery_sync(
    session_id: int,
    cutoff: datetime,
    now: datetime,
    expected_ember_session_id: str,
) -> dict | None:
    with Session(get_engine()) as db_session:
        return store.claim_hung_zombie_session_recovery(
            db_session,
            session_id,
            cutoff,
            now,
            expected_ember_session_id,
        )


async def _fresh_bound_guest_state(
    ember_session_id: str,
) -> tuple[bool, dict | None]:
    # router owns the shared all-lane VM cache. Import lazily because router
    # imports this module for execution helpers.
    from agent_sessions import router as agent_router

    return await agent_router._fresh_vm_state_for_binding(ember_session_id)


def _finalize_zombie_session_recovery_sync(claim: dict, now: datetime) -> int | None:
    with Session(get_engine()) as db_session:
        return store.finalize_zombie_session_recovery(db_session, claim, now)


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

    claim_owner = f"{_REPLICA_ID}:{uuid4()}"
    claimed_seq = await asyncio.to_thread(
        _claim_pending_message_sync, session_id, claim_owner
    )
    if claimed_seq is None:
        return
    _clear_negative_oracle_verdict(session_id)

    ui_originated = _consume_ui_originated(session_id, claimed_seq)

    # Set once the claim is lost to another replica; checked before starting and
    # again after deliver, so a stolen claim never persists a duplicate turn.
    claim_stolen = False
    claim_released = False
    stolen_exit_logged = False
    refresh_task = None

    def _abort_stolen_executor(stage: str) -> bool:
        nonlocal stolen_exit_logged
        if not claim_stolen:
            return False
        if not stolen_exit_logged:
            _log_stolen_exit(stage)
        return True

    async def _abort_stolen_executor_confirmed(stage: str) -> bool:
        """DB-confirmed barrier for WRITE paths.

        The local claim_stolen flag lags a steal by up to one heartbeat
        (10s), which is exactly the window a stolen executor could use to
        persist a binding or an error turn over the retry's work. Before any
        write that survives the executor, confirm ownership against the
        claim row itself.
        """
        nonlocal claim_stolen
        if claim_stolen:
            _log_stolen_exit(stage)
            return True
        still_held = await asyncio.to_thread(
            _refresh_claim_sync, session_id, claimed_seq, claim_owner
        )
        if still_held:
            return False
        claim_stolen = True
        _log_stolen_exit(stage)
        return True

    def _log_stolen_exit(stage: str) -> None:
        nonlocal stolen_exit_logged
        if not stolen_exit_logged:
            logger.warning(
                "Claim was stolen for turn %s in session %s during %s; executor writes nothing",
                claimed_seq,
                session_id,
                stage,
            )
            stolen_exit_logged = True

    async def _refresh_heartbeat() -> None:
        """Keep the claim alive while the turn runs."""
        nonlocal claim_stolen
        while not claim_stolen:
            try:
                await asyncio.sleep(10)  # one third of the 30s lease
                still_held = await asyncio.to_thread(
                    _refresh_claim_sync, session_id, claimed_seq, claim_owner
                )
                if not still_held:
                    claim_stolen = True
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
        if _abort_stolen_executor("startup"):
            return

        row = await asyncio.to_thread(
            _get_pending_message_sync, session_id, claimed_seq
        )
        if not row:
            return
        # Load session to get workspace and stored session_id for resumption
        session_row, _ = await asyncio.to_thread(_load_session, session_id)
        if not session_row:
            if _abort_stolen_executor("missing-session error"):
                return
            await asyncio.to_thread(
                _mark_turn_error_sync, session_id, claimed_seq, "Session not found"
            )
            _clear_negative_oracle_verdict(session_id)
            return
        if session_row.progress_token is None:
            session_row.progress_token = secrets.token_urlsafe(32)
            if _abort_stolen_executor("progress-token persistence"):
                return
            await asyncio.to_thread(
                store._persist_progress_token_sync,
                session_id,
                session_row.progress_token,
            )
        try:
            existing_ember = _ember_session(session_row)
            fresh_binding_persisted = False

            async def persist_callback(
                ember: EmberSession, cli_for_binding: str | None
            ) -> None:
                nonlocal fresh_binding_persisted
                if await _abort_stolen_executor_confirmed("binding persistence"):
                    raise _ClaimStolen
                if ember.workspace_lost:
                    await asyncio.to_thread(
                        _replace_ember_session_after_preemption,
                        session_id,
                        ember,
                    )
                else:
                    await asyncio.to_thread(
                        _persist_ember_session_and_cli,
                        session_id,
                        ember,
                        cli_for_binding,
                    )
                fresh_binding_persisted = True

            if existing_ember is None and session_row.prior_ember_lineage_id:
                # #4306 slice 5: the active binding is gone (a confirmed-dead
                # session, EmberSessionGone, or an admin destroy), but a
                # prior lineage survived the clear. Restore from it and
                # resume the PRIOR generation's CLI transcript rather than
                # starting a blank conversation despite the durable
                # workspace still existing.
                cli_session_id = session_row.prior_cli_session_id
                restore_from = session_row.prior_ember_lineage_id
            else:
                # Reuse the session_id from the database (from first turn), or None for new sessions
                cli_session_id = session_row.cli_session_id
                restore_from = None
            deliver_kwargs = {
                "restore_from": restore_from,
                "on_create": persist_callback,
                "progress_token": session_row.progress_token,
                "agent_session_id": session_id,
                "dispatch_count": row.dispatch_count,
            }
            if session_row.repo is not None:
                deliver_kwargs["repo"] = session_row.repo
                deliver_kwargs["branch"] = session_row.branch
            if session_row.system_prompt is not None:
                deliver_kwargs["system_prompt"] = session_row.system_prompt
            if session_row.reasoning:
                deliver_kwargs["reasoning"] = True
            effective_model = normalize_model(row.model)
            turn, ember = await _transport.deliver(
                existing_ember,
                cli_session_id,
                row.message_text,
                effective_model,
                **deliver_kwargs,
            )
            # Check if claim was stolen while deliver was running
            if _abort_stolen_executor("deliver"):
                return
            if ember != existing_ember:
                # Safety: re-persist for transports without on_create support
                await asyncio.to_thread(_persist_ember_session, session_id, ember)
        except _ClaimStolen:
            return
        except EmberBrickGone as exc:
            nonlocal claim_released
            if await _abort_stolen_executor_confirmed("brick_gone recovery"):
                return
            if row.dispatch_count > store.MAX_PENDING_DISPATCHES:
                await asyncio.to_thread(
                    _mark_turn_error_sync, session_id, claimed_seq, str(exc)
                )
            else:
                await asyncio.to_thread(
                    _mark_turn_interrupted_sync, session_id, claimed_seq
                )
                await asyncio.to_thread(
                    _release_pending_message_claim_sync,
                    session_id,
                    claimed_seq,
                    claim_owner,
                )
                claim_released = True
            _clear_negative_oracle_verdict(session_id)
            return
        except EmberSessionGone as exc:
            if await _abort_stolen_executor_confirmed("missing-guest error"):
                return
            # Session confirmed dead by CP; clear the binding and CLI id together.
            if not fresh_binding_persisted:
                await asyncio.to_thread(_clear_ember_session_sync, session_id)
            await asyncio.to_thread(
                _mark_turn_error_sync, session_id, claimed_seq, str(exc)
            )
            _clear_negative_oracle_verdict(session_id)
            return
        except Exception as exc:
            if await _abort_stolen_executor_confirmed("delivery error"):
                return
            # Terminal failure: row is deleted by mark_turn_error_sync (noqa: BLE001)
            await asyncio.to_thread(
                _mark_turn_error_sync, session_id, claimed_seq, str(exc)
            )
            _clear_negative_oracle_verdict(session_id)
            return

        summary = voice.extract_voice_summary(turn.result)
        status = _turn_status(turn)
        if await _abort_stolen_executor_confirmed("turn persistence"):
            return
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
                turn.model or effective_model,
            )
        except IntegrityError:
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
        _clear_negative_oracle_verdict(session_id)
        await asyncio.to_thread(_delete_pending_message_sync, session_id, claimed_seq)
        if not ui_originated:
            await _notify_terminal(turn, summary, status, session_row)
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
        if not claim_stolen and not claim_released:
            await asyncio.to_thread(
                _release_pending_message_claim_sync,
                session_id,
                claimed_seq,
                claim_owner,
            )


async def recover_zombie_session_if_needed(
    session_id: int, *, now: datetime | None = None
) -> dict | None:
    """Recover one old first-turn session if this pod wins its CAS."""
    if _negative_oracle_verdict_is_fresh(session_id):
        return None

    recovery_now = now or datetime.now(timezone.utc)
    cutoff = recovery_now - timedelta(seconds=ZOMBIE_SESSION_THRESHOLD_SECONDS)
    claim = await asyncio.to_thread(
        _claim_zombie_session_recovery_sync, session_id, cutoff, recovery_now
    )
    if claim is None:
        hung_cutoff = recovery_now - timedelta(seconds=HUNG_CLAIM_THRESHOLD_SECONDS)
        ember_session_id = await asyncio.to_thread(
            _get_hung_claim_binding_sync,
            session_id,
            hung_cutoff,
            recovery_now,
        )
        if ember_session_id is None:
            return None
        cp_available, vm_state = await _fresh_bound_guest_state(ember_session_id)
        if not cp_available:
            _memoize_negative_oracle_verdict(session_id)
            return None
        if vm_state is not None:
            never_invoked = vm_state.get("invoke_started_at") is None
            if not never_invoked:
                _memoize_negative_oracle_verdict(session_id)
                return None
        claim = await asyncio.to_thread(
            _claim_hung_zombie_session_recovery_sync,
            session_id,
            hung_cutoff,
            recovery_now,
            ember_session_id,
        )
        if claim is None:
            return None

    _clear_negative_oracle_verdict(session_id)
    ember_session_id = claim["ember_session_id"]
    if ember_session_id:
        try:
            await _transport.destroy_session(ember_session_id)
        except Exception:  # noqa: BLE001 - binding clear is the recovery backstop
            # A dead or unreachable application version commonly leaves a
            # control-plane id that is already gone. The durable binding must
            # still be cleared so the retry can create or restore a guest.
            logger.warning(
                "Could not destroy zombie EmberVM session %s; clearing its binding",
                ember_session_id,
                exc_info=True,
            )
        await asyncio.to_thread(_clear_ember_bindings_for, ember_session_id)

    if claim.get("recovery_declined"):
        logger.error(
            "Stopped zombie recovery for session %s turn %s after too many dispatches",
            session_id,
            claim["turn_seq"],
        )
        return claim

    retry_seq = await asyncio.to_thread(
        _finalize_zombie_session_recovery_sync,
        claim,
        datetime.now(timezone.utc),
    )
    if retry_seq is None:
        logger.warning(
            "Zombie recovery lost ownership for session %s turn %s",
            session_id,
            claim["turn_seq"],
        )
        return None
    logger.warning(
        "Recovered zombie session %s turn %s in place (workspace_loss=%s)",
        session_id,
        claim["turn_seq"],
        claim["recovery_workspace_loss"],
    )
    # Discord echo is accepted: the recovered turn's process-local UI mark died with its pod.
    _schedule_next_message(session_id)
    return {**claim, "retry_seq": retry_seq}


async def _reconcile_zombie_sessions() -> int:
    """Recover a bounded batch of eligible first-turn lane heads.

    Hung claims still require a fresh control-plane answer in
    recover_zombie_session_if_needed before their CAS can run.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=ZOMBIE_SESSION_THRESHOLD_SECONDS)
    hung_cutoff = now - timedelta(seconds=HUNG_CLAIM_THRESHOLD_SECONDS)
    unclaimed_ids, hung_ids = await asyncio.gather(
        asyncio.to_thread(_find_zombie_session_ids_sync, cutoff, now, 5),
        asyncio.to_thread(_find_hung_claim_session_ids_sync, hung_cutoff, now, 5),
    )
    session_ids = list(dict.fromkeys([*unclaimed_ids, *hung_ids]))[:5]
    if not session_ids:
        return 0
    recovered = 0
    for session_id in session_ids:
        try:
            result = await recover_zombie_session_if_needed(session_id, now=now)
        except Exception as exc:  # noqa: BLE001 - one recovery must not stop the sweep
            logger.error("Zombie recovery failed for session %s: %s", session_id, exc)
            continue
        if result is not None and result.get("retry_seq") is not None:
            recovered += 1
    return recovered


async def _sweep_orphaned_pending_messages() -> None:
    """Pick up pending messages left behind by a crash or restart.

    The sweep does two things:
    1. Reclaim any claims that have expired (replica crashed), making them
       available for re-execution by the current leader.
    2. Execute any pending messages that are not yet claimed.
    """
    while True:
        recovered = await _reconcile_zombie_sessions()
        if recovered > 0:
            logger.warning("Recovered %d zombie agent sessions", recovered)
        await asyncio.sleep(5)
        # Reclaim stale claims from crashed replicas
        reclaimed = await asyncio.to_thread(_reclaim_stale_claims_sync)
        if reclaimed > 0:
            logger.info("Reclaimed %d stale claims from crashed replicas", reclaimed)
        # Skip sessions paused for device login; a persisted prompt must not
        # execute until the grant is live, else the turn fails with opaque 422
        # within 5 seconds.
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
async def monolith_agent_session_start(
    prompt: str,
    model: str | None = None,
    repo: str = DEFAULT_AGENT_REPO,
    branch: str = "main",
    reasoning: bool | None = None,
) -> dict:
    """Start a voice-drivable coding agent session and queue its first turn.

    Args:
        prompt: The first message for the session.
        model: Optional model, which also pins the session's adapter family.
            Claude family: opus, sonnet, fable. Codex family: luna, terra,
            sol. Pi family: spark. The deprecated qwen alias is accepted. Omit
            for the claude CLI default. Later
            sends may only name models within the pinned family.
        repo: owner/repo to check out into the guest workspace, defaulting to
            jomcgi-org/homelab. Must be in the catalog. Pass an empty string for a
            session with NO checkout, which starts faster and suits a session
            that only talks.
        branch: Branch to check out. Defaults to main.
        reasoning: Optional reasoning preference retained across the session.
            Muse Spark currently ignores this legacy Pi hint.
    """
    try:
        selected_model = normalize_model(model)
        model_family(selected_model)
    except ValueError as exc:
        return {"accepted": False, "error": str(exc)}
    # Catalog-gated on the same rule the /agents route uses, and for a stronger
    # reason here: this value reaches the guest and is interpolated into a clone
    # URL on a credentialed egress path, so an arbitrary string would be a
    # fetch-anything primitive carrying a real GitHub token.
    selected_repo = repo.strip() or None
    if selected_repo is not None and selected_repo not in REPO_CATALOG:
        return {
            "accepted": False,
            "error": f"unknown repo {selected_repo}; catalog: {', '.join(REPO_CATALOG)}",
        }
    local_session_id = str(uuid4())
    workspace = "<guest>"  # Workspace is in the guest, not the pod
    row = await asyncio.to_thread(
        _persist_session,
        local_session_id,
        workspace,
        branch,
        selected_model,
        selected_repo,
        discord_thread=None,
        system_prompt=_append_rationale_trailer(voice.VOICE_INSTRUCTION, selected_repo),
        prompt=prompt,
        # Unset means decide from repo presence, matching the /agents route.
        reasoning=bool(selected_repo) if reasoning is None else reasoning,
    )
    turn = await asyncio.to_thread(
        _persist_pending_message, row.id, prompt, selected_model
    )
    _schedule_next_message(row.id)
    return {"accepted": True, "session_id": row.id, "turn": turn}


def _mint_voice_ui_session() -> AgentSession:
    return _persist_session(
        str(uuid4()),
        "<guest>",
        "main",
        None,
        DEFAULT_AGENT_REPO,
        discord_thread=None,
        system_prompt=_append_rationale_trailer(
            voice.VOICE_INSTRUCTION, DEFAULT_AGENT_REPO
        ),
    )


def _voice_ui_principal() -> tuple[str, str]:
    principal = current_principal()
    # Principal facts are recorded, never used as a gate. ADR 058 deliberately
    # keeps the voice path available when MCP carries an anonymous principal.
    return principal.subject, str(principal.authority)


@mcp.tool
async def monolith_voice_ui_attach(session_id: int | None = None) -> dict:
    """Bind the open voice UI companion to an existing or new agent session."""
    subject, authority = _voice_ui_principal()
    return await asyncio.to_thread(
        voice_ui.attach,
        session_id,
        subject,
        authority,
        _mint_voice_ui_session,
    )


@mcp.tool
async def monolith_voice_ui_show(
    surface: str, ref: str, focus: str | None = None
) -> dict:
    """Show one run, walkthrough, transcript, or VM surface on the companion."""
    subject, authority = _voice_ui_principal()
    return await asyncio.to_thread(
        voice_ui.show, surface, ref, focus, subject, authority
    )


@mcp.tool
async def monolith_voice_ui_ask(
    question: str,
    options: list[str],
    ref: str,
    node_key: str | None = None,
) -> dict:
    """Record a companion question and return immediately.

    Pass node_key to target an open run decision. Otherwise the answer goes to
    the attached session.
    """
    subject, authority = _voice_ui_principal()
    return await asyncio.to_thread(
        voice_ui.ask, question, options, ref, subject, authority, node_key
    )


@mcp.tool
async def monolith_voice_ui_dismiss(surface: str | None = None) -> dict:
    """Dismiss one companion surface, or the current surface when omitted."""
    subject, authority = _voice_ui_principal()
    return await asyncio.to_thread(voice_ui.dismiss, surface, subject, authority)


def _record_swarm_decision(
    workflow_id: str,
    node_key: str,
    decision: str,
    note: str | None,
    actor_subject: str,
    actor_authority: str,
) -> dict:
    from swarm import store as swarm_store

    with Session(get_engine()) as session:
        idempotent = (
            swarm_store.get_open_decision(session, workflow_id, node_key) is None
        )
        row = swarm_store.record_decision(
            session,
            workflow_id,
            node_key,
            decision,
            note,
            actor_subject,
            actor_authority,
        )
        return swarm_store.decision_response(row, idempotent)


@mcp.tool
async def monolith_agent_run_decide(
    workflow_id: str,
    node_key: str,
    decision: str,
    note: str | None = None,
) -> dict:
    """Record a decision for a paused swarm run and return immediately.

    Args:
        workflow_id: The swarm workflow waiting for a decision.
        node_key: The blocked node named by the decision request.
        decision: One of the options recorded on the open decision row.
        note: Optional context to record with the decision.
    """
    from swarm.store import InvalidDecision, NoOpenDecision

    principal = current_principal()
    # The HTTP path sits behind Cloudflare Access. Requiring an identified MCP
    # principal provides the equivalent authorization floor for this path.
    if principal.authority is Authority.ANONYMOUS:
        return {
            "accepted": False,
            "error": "an identified caller is required to decide",
        }
    if note is not None and len(note) > 2000:
        return {"accepted": False, "error": "note must be at most 2000 characters"}
    try:
        return await asyncio.to_thread(
            _record_swarm_decision,
            workflow_id,
            node_key,
            decision,
            note,
            principal.subject,
            str(principal.authority),
        )
    except NoOpenDecision:
        return {"accepted": False, "error": "no open decision for this node"}
    except InvalidDecision as exc:
        return {"accepted": False, "error": str(exc)}


@mcp.tool
async def monolith_agent_session_send(
    session_id: int, message: str, model: str | None = None
) -> dict:
    """Enqueue a message for a session, returning once accepted rather than once complete.

    Args:
        session_id: The session to send to.
        message: The message text for the next turn.
        model: Optional per-turn model within the session's pinned family.
            Claude family: opus, sonnet, fable. Codex family: luna, terra,
            sol. Pi family: spark. The deprecated qwen alias is accepted.
            Defaults to the session's model.
    """
    row = await asyncio.to_thread(_load_session_row, session_id)
    if row is None:
        return {"accepted": False, "error": f"Unknown agent session {session_id}"}
    try:
        # Both lookups stay inside the try: a session pinned to a model whose
        # alias has since been retired must produce the readable rejection, not
        # an uncaught ValueError out of the tool.
        session_model = normalize_model(row.model)
        requested_model = normalize_model(model)
        session_family = model_family(session_model)
        requested_family = (
            model_family(requested_model)
            if requested_model is not None
            else session_family
        )
    except ValueError as exc:
        return {"accepted": False, "error": str(exc)}
    if requested_family != session_family:
        return {
            "accepted": False,
            "error": (
                f"Model family mismatch: session family is {session_family}, "
                f"requested model family is {requested_family}"
            ),
        }
    effective_model = requested_model or session_model
    try:
        await recover_zombie_session_if_needed(session_id)
    except Exception:  # noqa: BLE001 - recovery cannot reject or lose a send
        logger.exception("Recovery check failed for session %s", session_id)
    try:
        turn = await asyncio.to_thread(
            _persist_pending_message, session_id, message, effective_model
        )
    except Exception:  # noqa: BLE001 - MCP gates return structured failures
        logger.exception("Could not persist message for session %s", session_id)
        return {
            "accepted": False,
            "error": "Could not queue message",
            "session_id": session_id,
        }
    try:
        activated = await asyncio.to_thread(_activate_session_after_enqueue, session_id)
    except Exception:  # noqa: BLE001 - the durable queue remains the backstop
        logger.exception("Could not activate queued session %s", session_id)
        activated = False
    if activated:
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
        "model": row.model,
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
    return {
        "prompt": selected.prompt,
        "prompt_intent": selected.prompt_intent,
        "result_text": selected.result_text,
        "rationale": parse_rationale(selected.result_text),
        "model": selected.model,
        "activities": _activities(selected),
    }


@mcp.tool
async def monolith_agent_session_vms(
    limit: int = 50, offset: int = 0, workload: str | None = None
) -> dict:
    """List the EmberVM session VMs holding a workload's session slots.

    Parked sessions count with banked toward session.maxSessions (the disk
    bucket) and no longer hold a concurrency.cap slot. Stale parked sessions
    can still exhaust maxSessions and deny every new create with a
    session_cap 429. This lists the slot holders (state, timestamps, expiry)
    either way, so the stale ones can be destroyed with
    monolith-agent-session-destroy.

    Args:
        workload: Which lane to list, defaults to the claude runtime. Pass
            "pi-runtime" to see the spark family's lane instead. The two
            lanes are never aggregated, since the session cap is per
            workload.
    """
    try:
        return await _transport.list_sessions(
            limit=limit, offset=offset, workload=workload
        )
    except EmberVMTransportError as exc:
        return {"error": str(exc)}


@mcp.tool
async def monolith_agent_session_destroy(ember_session_id: str) -> dict:
    """Destroy one EmberVM session VM, freeing its workload cap slot.

    Args:
        ember_session_id: The control plane session id (s-...), from
            monolith-agent-session-vms or a session's stored binding.

    Destroying a session an in-flight turn is using makes that turn fail,
    so this is intended for stale or parked test sessions. Any monolith agent session
    bound to the destroyed id has its binding cleared so the next send
    creates a fresh EmberVM session instead of invoking a dead one.
    """
    try:
        result = await _transport.destroy_session(ember_session_id)
    except EmberVMTransportError as exc:
        return {"error": str(exc)}
    cleared = await asyncio.to_thread(_clear_ember_bindings_for, ember_session_id)
    result["cleared_bindings"] = cleared
    return result


# -- token broker login (ADR 048, #4250 PR 2) --------------------------------

BROKER_URL_ENV = "EMBER_TOKENBROKER_URL"
_DEFAULT_GRANT = "codex-cluster"
_GRANT_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def _broker_url() -> str:
    url = os.environ.get(BROKER_URL_ENV, "")
    if not url:
        raise ValueError("token broker is not configured")
    return url.rstrip("/")


def _grant_or_raise(grant: str) -> str:
    # The grant rides a URL path and names a Kubernetes Secret suffix.
    if not _GRANT_NAME_RE.fullmatch(grant):
        raise ValueError(f"invalid grant name {grant!r}")
    return grant


async def _broker_request(method: str, path: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(method, _broker_url() + path)
        resp.raise_for_status()
        return resp.json()


@mcp.tool
async def monolith_codex_broker_login_start(grant: str = _DEFAULT_GRANT) -> dict:
    """Begin the token broker device-code login for an OAuth grant.

    Returns the verification URL and one-time user code to approve in any
    browser. The broker polls until the grant is approved and persists the
    token bundle durably. Never share a device code with anyone.

    Args:
        grant: Grant name registered in the broker. Defaults to codex-cluster.
    """
    grant = _grant_or_raise(grant)
    data = await _broker_request("POST", f"/grants/{grant}/login/start")
    await agent_api.notify(
        "codex broker login pending for grant %s. Approve code %s at %s"
        % (grant, data.get("user_code", "?"), data.get("verification_url", "?")),
        level="warn",
    )
    return data


@mcp.tool
async def monolith_codex_broker_refresh(grant: str = _DEFAULT_GRANT) -> dict:
    """Force the token broker to rotate an OAuth grant's access token.

    This bypasses the normal freshness windows for a token that a destination
    invalidated before its stored expiry. The response contains rotation status
    only and never returns the access token.

    Args:
        grant: Grant name registered in the broker. Defaults to codex-cluster.
    """
    grant = _grant_or_raise(grant)
    try:
        data = await _broker_request("POST", f"/grants/{grant}/refresh")
        data.pop("access_token", None)
        return data
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            return {"refreshed": False, "reason": "cooldown"}
        if exc.response.status_code == httpx.codes.SERVICE_UNAVAILABLE:
            data = exc.response.json()
            data.pop("access_token", None)
            data["refreshed"] = False
            if data.get("needs_login"):
                await agent_api.notify(
                    "codex broker grant %s requires a device login before it can refresh."
                    % grant,
                    level="warn",
                )
            return data
        raise


@mcp.tool
async def monolith_codex_broker_login_status(grant: str = _DEFAULT_GRANT) -> dict:
    """Report the token broker login state for an OAuth grant.

    Args:
        grant: Grant name registered in the broker. Defaults to codex-cluster.
    """
    grant = _grant_or_raise(grant)
    data = await _broker_request("GET", f"/grants/{grant}/login/status")
    if data.get("state") == "granted":
        await agent_api.notify(
            "codex broker grant %s is active. The lane refreshes itself from here."
            % grant,
            level="info",
        )
    return data
