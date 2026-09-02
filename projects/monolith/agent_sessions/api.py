"""Public API for Discord and other external agent session callers."""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from agent_sessions import model_family, store
from agent_sessions.codex_login import codex_login_gate, watch_for_login
from agent_sessions.constants import SYNTHETIC_SESSION_PREFIX
from agent_sessions.mcp import (
    _append_rationale_trailer,
    _claim_pending_message_sync,
    _clear_ember_bindings_for,
    _delete_pending_message_sync,
    _load_session_row,
    _mark_turn_error_sync,
    _persist_ember_session,
    _persist_pending_message,
    _persist_session,
    _persist_turn_from_pending_sync,
    _REPLICA_ID,
    _refresh_claim_sync,
    _release_pending_message_claim_sync,
    _schedule_next_message,
    _set_session_status,
    _transport,
)
from agent_sessions.transport import EmberSessionGone
from core.db import get_engine
from goosecracker.api import REPO_CATALOG

logger = logging.getLogger(__name__)


async def run_synthetic_session(prompt: str, model: str = "luna"):
    """Run and persist one short synthetic session through the normal path.

    This is intentionally a direct, awaited form of the session worker for
    probes. It still uses the shared transport and turn persistence, but gives
    the caller the completed turn instead of requiring a background task and a
    database poll. The session is always destroyed before returning.
    """
    from agent_sessions.mcp import _turn_status

    model_family(model)
    row = await asyncio.to_thread(
        _persist_session,
        f"{SYNTHETIC_SESSION_PREFIX}{uuid4()}",
        "<guest>",
        "main",
        model,
        None,
    )
    assert row.id is not None
    turn_seq = await asyncio.to_thread(_persist_pending_message, row.id, prompt, model)

    # The monolith runs multiple replicas; an unclaimed pending message is fair
    # game for another replica's worker. Claiming it here ensures the prompt is
    # delivered at most once per probe run.
    claimed_seq = await asyncio.to_thread(_claim_pending_message_sync, row.id)
    if claimed_seq is None:
        logger.warning(
            "Pending turn %s for synthetic session %s was claimed by another replica",
            turn_seq,
            row.id,
        )
        await asyncio.to_thread(_delete_pending_message_sync, row.id, turn_seq)
        return None

    # The claim lease is 30 seconds. An observed run on 2026-09-02 06:00:27 took
    # about 27 seconds end to end, leaving only a 3 second margin. A cold guest
    # boot or relight would exceed that, causing the claim to expire mid-delivery
    # and another replica to reclaim, reintroducing double delivery. Refresh the
    # claim every 10 seconds against the 30 second lease, reusing the same
    # mechanism as the worker path (mcp.py:416-440).
    claim_stolen = False

    async def _refresh_heartbeat() -> None:
        nonlocal claim_stolen
        while not claim_stolen:
            try:
                await asyncio.sleep(10)
                still_held = await asyncio.to_thread(
                    _refresh_claim_sync, row.id, turn_seq, _REPLICA_ID
                )
                if not still_held:
                    claim_stolen = True
                    logger.warning(
                        "Claim for turn %s in session %s was reclaimed",
                        turn_seq,
                        row.id,
                    )
                    break
            except Exception:
                logger.exception(
                    "Failed to refresh claim for turn %s in session %s",
                    turn_seq,
                    row.id,
                )

    refresh_task = asyncio.create_task(_refresh_heartbeat())
    ember = None

    async def persist_callback(created_ember, _cli_session_id):
        nonlocal ember
        # Capture the created session before the persistence hop so a failed
        # invoke or callback still reaches the cleanup block below.
        ember = created_ember
        await asyncio.to_thread(_persist_ember_session, row.id, created_ember)

    try:
        if claim_stolen:
            await asyncio.to_thread(_delete_pending_message_sync, row.id, turn_seq)
            logger.warning(
                "Claim for turn %s in session %s was reclaimed before delivery, aborting",
                turn_seq,
                row.id,
            )
            return None
        turn, _returned_ember = await _transport.deliver(
            None,
            None,
            prompt,
            model,
            on_create=persist_callback,
        )
        ember = _returned_ember
        if claim_stolen:
            logger.warning(
                "Claim was stolen during deliver for turn %s in session %s, not persisting result",
                turn_seq,
                row.id,
            )
            await asyncio.to_thread(_delete_pending_message_sync, row.id, turn_seq)
            return None
        status = _turn_status(turn)
        try:
            await asyncio.to_thread(
                _persist_turn_from_pending_sync,
                row.id,
                turn_seq,
                prompt,
                turn,
                turn.result.strip()[:200],
                status,
                turn.session_id,
                model,
            )
        except IntegrityError:
            logger.warning(
                "Turn %s for session %s already exists (duplicate seq), discarding retry",
                turn_seq,
                row.id,
            )
            await asyncio.to_thread(_delete_pending_message_sync, row.id, turn_seq)
            return turn
        await asyncio.to_thread(_delete_pending_message_sync, row.id, turn_seq)
        return turn
    except Exception as exc:
        # Transport errors already contain _status_error_detail when the
        # control plane supplied a response body. Persist that same detail on
        # the failed turn, not only in the transport log.
        await asyncio.to_thread(_mark_turn_error_sync, row.id, turn_seq, str(exc))
        raise
    finally:
        refresh_task.cancel()
        await asyncio.to_thread(
            _release_pending_message_claim_sync, row.id, claimed_seq
        )
        if ember is not None:
            try:
                await _transport.destroy_session(ember.session_id)
            except EmberSessionGone:
                pass
            finally:
                await asyncio.to_thread(_clear_ember_bindings_for, ember.session_id)


def start_session_for_swarm(
    local_session_id: str,
    prompt: str,
    model: str,
    repo: str,
    branch: str,
    workflow_id: str | None = None,
    node_key: str | None = None,
    node_attempt: int | None = None,
    reasoning: bool = False,
) -> int:
    """Create and schedule a swarm-owned session through the normal session path.

    ``local_session_id`` is the caller's IDEMPOTENCY KEY, not a fresh uuid. DBOS
    steps are at-least-once, so a retried step that minted a new id each time
    would leave one live agent session per attempt, each burning a Codex slot.
    A repeat call with the same key returns the existing session untouched.

    Safe to call from a non-async thread. DBOS executes sync steps on a worker
    thread with no running event loop, where ``_schedule_next_message`` cannot
    ``asyncio.create_task``; the leader's orphan sweep (every 5s) picks the
    pending message up instead, so scheduling here is an optimisation rather
    than the liveness mechanism.
    """
    if repo not in REPO_CATALOG:
        raise ValueError(f"unknown repo {repo}; catalog: {', '.join(REPO_CATALOG)}")
    model_family(model)
    with Session(get_engine()) as session:
        existing = store.get_session_by_local_id(session, local_session_id)
    if existing is not None and existing.id is not None:
        return existing.id
    row = _persist_session(
        local_session_id,
        "<guest>",
        branch,
        model,
        repo,
        reasoning=reasoning,
        workflow_id=workflow_id,
        node_key=node_key,
        node_attempt=node_attempt,
    )
    assert row.id is not None
    _persist_pending_message(row.id, prompt, model)
    try:
        _schedule_next_message(row.id)
    except RuntimeError:
        # No running event loop: this is a DBOS worker thread. The orphan sweep
        # will claim and execute the pending message within ~5s.
        pass
    return row.id


def _sessions_for_workflow(workflow_id: str):
    with Session(get_engine()) as db_session:
        return store.sessions_for_workflow(db_session, workflow_id)


async def reap_sessions_for_workflow(workflow_id: str) -> dict:
    """Destroy and unbind every guest session owned by a swarm workflow.

    Every list keys on the MONOLITH session id, never the EmberVM id, so a
    caller reading the summary can look each one up in /agents directly. One
    session failing never stops the others: a cancelled run that leaves even
    one guest alive keeps burning a slot against the live-capacity cap.

    "Already gone" is decided by ``EmberSessionGone``, which the transport
    raises off the STATUS CODE. Sniffing the message string instead would read
    a 500 whose URL or body merely contains "404" as success and leak the very
    slot this reap exists to reclaim.
    """
    rows = await asyncio.to_thread(_sessions_for_workflow, workflow_id)
    summary: dict[str, list] = {"reaped": [], "failed": [], "skipped": []}
    for row in rows:
        ember_session_id = row.ember_session_id
        if ember_session_id is None:
            summary["skipped"].append(row.id)
            continue
        try:
            try:
                await _transport.destroy_session(ember_session_id)
            except EmberSessionGone:
                # The goal state, not a failure: still clear the binding below.
                pass
            await asyncio.to_thread(_clear_ember_bindings_for, ember_session_id)
            summary["reaped"].append(row.id)
        except Exception as exc:  # noqa: BLE001 - one bad session must not stop the rest
            # Logged as well as returned: a caller that drops the response would
            # otherwise leave a permanently leaked capacity slot with no trace.
            logger.warning(
                "swarm reap failed for session %s (ember %s) of workflow %s: %s",
                row.id,
                ember_session_id,
                workflow_id,
                exc,
            )
            summary["failed"].append({"session_id": row.id, "error": str(exc)})
    return summary


async def start_session_for_thread(
    thread_id: str, prompt: str, repo: str | None, model: str = "luna"
) -> int | dict:
    """Create a model session bound to a Discord thread and queue its first turn.

    ``thread_id`` identifies the Discord thread that should receive terminal
    notifications, ``prompt`` is the initial user request, ``repo`` selects
    the checkout or leaves it empty for an artifact run, and ``model`` selects
    the adapter family.  The returned integer is the durable session id, which
    callers use to correlate later turns and database records.
    """
    if repo is not None and repo not in REPO_CATALOG:
        raise ValueError(f"unknown repo {repo}; catalog: {', '.join(REPO_CATALOG)}")
    model_family(model)
    row = await asyncio.to_thread(
        _persist_session,
        str(uuid4()),
        "<guest>",
        "main",
        model,
        repo,
        discord_thread=thread_id,
        system_prompt=_append_rationale_trailer(None, repo),
    )
    await asyncio.to_thread(_persist_pending_message, row.id, prompt, model)
    login = await codex_login_gate(model)
    if login is not None:
        await asyncio.to_thread(_set_session_status, row.id, "awaiting_login")

        # Keep the thread binding and original prompt durable while the owner
        # approves the device code. The watcher starts this exact pending turn
        # once the broker reports granted.
        async def resume() -> None:
            await asyncio.to_thread(_set_session_status, row.id, "running")
            _schedule_next_message(row.id)

        watch_for_login(login.get("grant", "codex-cluster"), resume)
        return login
    _schedule_next_message(row.id)
    return row.id


async def send_to_thread_session(thread_id: str, message: str) -> dict | None:
    """Queue a follow-up message for the session bound to a Discord thread.

    ``thread_id`` scopes the message to an existing thread-owned session and
    ``message`` is the owner's next turn.  The result describes the queued turn,
    or is ``None`` when no session is bound, so callers can fall through to
    ordinary chat handling instead of claiming an unrelated thread.

    Async, and deliberately not a sync function the caller wraps in
    ``asyncio.to_thread``: ``_schedule_next_message`` calls
    ``asyncio.create_task``, which needs a running event loop on the calling
    thread. Off the loop it raises RuntimeError AFTER the turn is already
    persisted, so the owner would see a send failure for a turn that the 5s
    sweep then runs anyway. The blocking database calls get their own
    ``to_thread`` hops instead, matching ``start_session_for_thread``.
    """
    session_id = await asyncio.to_thread(session_id_for_thread, thread_id)
    if session_id is None:
        return None
    row = await asyncio.to_thread(_load_session_row, session_id)
    if row is None:
        return None
    model_family(row.model)
    # row.model, NOT None. None is not "unset", it resolves to the CLAUDE family
    # (model_family(None) == "claude"), so a None here ran the claude adapter
    # against a session whose CLI transcript belongs to codex and died with
    # "claude exited before init / No conversation found with session ID".
    # Every follow-up turn must stay inside the family the session pinned.
    turn = await asyncio.to_thread(
        _persist_pending_message, session_id, message, row.model
    )
    login = await codex_login_gate(row.model)
    if login is not None:
        await asyncio.to_thread(_set_session_status, session_id, "awaiting_login")

        async def resume() -> None:
            await asyncio.to_thread(_set_session_status, session_id, "running")
            _schedule_next_message(session_id)

        watch_for_login(login.get("grant", "codex-cluster"), resume)
        return login
    await asyncio.to_thread(_set_session_status, session_id, "running")
    _schedule_next_message(session_id)
    return {"action": "queued", "session_id": session_id, "turn": turn}


def session_id_for_thread(thread_id: str) -> int | None:
    """Find the durable session id for a Discord thread, if one is bound.

    ``thread_id`` is the Discord thread identifier.  Returning ``None`` for an
    unbound thread lets message routing distinguish agent turns from normal
    conversation without creating session state as a side effect.
    """
    with Session(get_engine()) as db_session:
        row = store.get_session_by_discord_thread(db_session, thread_id)
        return row.id if row else None
