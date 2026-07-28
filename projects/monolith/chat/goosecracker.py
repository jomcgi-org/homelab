"""goosecracker: the Discord coding-agent session logic (ADR 024 / ADR 035).

The ``/agent`` command opens a Discord thread and runs goose in a Firecracker
microVM; the router classifies the task and delegates a sub-recipe (query, plan,
implement, or artifact-build/review for a repo-less "build me a page" run), and
the result is posted back into the thread. Each owner follow-up in the thread
continues the same agent session: it steers an in-flight turn or dispatches the
next one (ADR 035 Phase 2).

This module is the pure logic seam (gate + transcript + steering + dispatch +
roast) so the Discord wiring in ``chat.bot`` stays thin and this stays
unit-testable. The DB helpers are synchronous and open their own session, so the
bot calls them via ``asyncio.to_thread`` (never blocking the gateway loop).
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlmodel import Session, select

from core.db import get_engine
from chat.models import GoosecrackerSession, GoosecrackerSteering, Message

if TYPE_CHECKING:
    from chat import orchestrator_plan

logger = logging.getLogger(__name__)

# Reaction lifecycle emojis for queued agent replies. A reply queued behind a
# running turn is acknowledged with QUEUED (no noisy text message); when its turn
# starts it flips to RUNNING, and on completion to DONE (or FAILED). Driven by the
# runner via the Discord outbox reaction verb (chat.outbox.enqueue_reaction).
REACTION_QUEUED = "⏳"  # ⏳
REACTION_RUNNING = "\U0001f440"  # 👀
REACTION_DONE = "✅"  # ✅
REACTION_FAILED = "❌"  # ❌

# Per-process boot token. Every turn stamps the session's runner_instance with
# this; a running session whose runner_instance differs from the live process's
# token was owned by a process that has since died (in-process daemon threads do
# not survive a restart), so it is safe to reclaim. Precise "it reset" signal
# with no external pod-health probe: the owner identity is the liveness proof.
INSTANCE_TOKEN = secrets.token_hex(8)

# A running turn older than this is presumed dead (the owning process wedged or
# died without the startup sweep catching it). Generous: well above the fc-invoke
# run cap (~600s) plus retries, so a genuinely slow turn is never reclaimed out
# from under itself. The startup sweep is the fast path; this is the backstop.
STALE_AFTER = timedelta(minutes=30)

# The agent recipe + tier: default (empty) tier runs on the in-cluster Qwen model.
# The agent recipe is the general coding agent (goosecracker general mode); an
# artifact is just an agent run with no repo, which the router builds by
# delegating the artifact-build / artifact-review sub-recipes.
AGENT_RECIPE = "agent"
AGENT_TIER = ""

# Shown when the owner gate rejects someone and the qwen roast path is
# unavailable (model down), so a non-owner always gets a clear refusal.
_FALLBACK_ROAST = "Nice try. You need an agent grant to drive that thread."


def owner_id() -> str:
    """The configured owner Discord user id, or "" when unset."""
    return os.environ.get("OWNER_DISCORD_USER_ID", "")


def is_owner(user_id: int | str) -> bool:
    """True only when an owner id is configured and matches.

    Fails closed: an unset OWNER_DISCORD_USER_ID rejects everyone rather than
    opening the agent (which runs arbitrary code in a microVM and spends model
    budget) to the whole server.
    """
    owner = owner_id()
    return bool(owner) and str(user_id) == owner


def _join_transcript(existing: str, message: str) -> str:
    """Append an owner turn to the curated transcript."""
    message = message.strip()
    if not existing:
        return message
    return f"{existing}\n\n{message}"


def _append_pending(existing: str, msg: str) -> str:
    """Join a new reply onto the pending queue (newline-separated).

    Used by the agent conversational path to accumulate replies that arrive
    while a turn is running; the queue is drained as a single next-turn task
    when the current turn finishes.
    """
    if not existing:
        return msg
    return f"{existing}\n{msg}"


def _append_id(existing: str, msg_id: str) -> str:
    """Append a Discord message id to a newline-joined id list."""
    if not existing:
        return msg_id
    return f"{existing}\n{msg_id}"


def _merge_ids(*id_lists: str) -> str:
    """Merge newline-joined id lists, dropping blanks, preserving order."""
    ids: list[str] = []
    for chunk in id_lists:
        ids.extend(i for i in chunk.split("\n") if i)
    return "\n".join(ids)


def _split_ids(joined: str) -> list[str]:
    """Split a newline-joined id list into non-empty ids."""
    return [i for i in joined.split("\n") if i]


def _is_stale(row: GoosecrackerSession, now: datetime) -> bool:
    """True when ``row`` is a running turn presumed dead (stale or foreign owner).

    A row is reclaimable when it is running AND either owned by a different
    process (the owner died: its in-process turn cannot exist here) or older than
    STALE_AFTER (wedged without a restart). ``running_since`` naive from SQLite is
    coerced to UTC so the comparison is offset-consistent in tests and prod.
    """
    if not row.running:
        return False
    if row.runner_instance and row.runner_instance != INSTANCE_TOKEN:
        return True
    since = row.running_since
    if since is None:
        # Running with no timestamp (legacy row / pre-migration): treat as stale
        # so it can never wedge the thread forever.
        return True
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return (now - since) > STALE_AFTER


def set_progress_message(thread_id: str, message_id: str) -> None:
    """Record the id of the run's single live message on the session row.

    The bot posts one live message per turn (the "🤖 Planning…" reply it edits in
    place with the checklist) and stamps its id here so the off-loop runner can
    later overwrite that same message with the final result via a durable outbox
    edit (one message, not two). A no-op when the thread has no session row (e.g.
    an artifact run with no persisted session). Sync; call via asyncio.to_thread.
    """
    with Session(get_engine()) as session:
        row = session.get(GoosecrackerSession, thread_id, with_for_update=True)
        if row is None:
            return
        row.progress_message_id = message_id
        session.add(row)
        session.commit()


def take_progress_message(thread_id: str) -> str:
    """Read AND clear the run's live message id for ``thread_id`` atomically.

    Consume-on-read: the runner takes the id when it settles a turn's result into
    the message, and clears it in the same transaction so the id is used at most
    once. A later turn in the same run (the conversational drain reuses one
    run_and_deliver call and posts no fresh live message) then reads '' and posts
    its own result rather than overwriting the earlier turn's. Returns '' when
    there is no row or no live message. Sync; call via asyncio.to_thread.
    """
    with Session(get_engine()) as session:
        row = session.get(GoosecrackerSession, thread_id, with_for_update=True)
        if row is None:
            return ""
        message_id = row.progress_message_id
        if message_id:
            row.progress_message_id = ""
            session.add(row)
            session.commit()
        return message_id


def continue_session(
    thread_id: str,
    message: str,
    message_id: str = "",
    author_id: str = "",
    tier: str = "",
) -> dict | None:
    """Append an owner follow-up to an agent thread and steer, queue, or dispatch.

    Conversational queuing. If a turn is already running (and not stale), the reply
    becomes STEERING (ADR 035 Phase 2): an undelivered ``GoosecrackerSteering`` row
    is inserted for the running guest to consume mid-run, and
    ``{"action": "steering"}`` is returned so the bot can acknowledge with a 👀
    reaction instead of a text reply. ``author_id``/``tier`` attribute the steering
    row (tier falls back to the session's own tier when blank). When idle (or the
    prior turn is stale and reclaimed), build the next task from any backlog + this
    message, set running=True, dispatch, and return
    ``{"action": "dispatched", ...submit result}``.

    Returns None when ``thread_id`` is not a goosecracker thread so the caller
    can fall through to normal chat handling. Synchronous; call via
    asyncio.to_thread.
    """
    message = message.strip()
    # Captured inside the session for use after the with-block.
    _task = ""
    _stored_recipe = ""
    _stored_tier = ""
    _stored_repo = ""
    _stored_provider = "discord"

    with Session(get_engine()) as session:
        # Lock the row for the whole read-modify-write so a concurrent
        # drain_agent_queue (runner thread) cannot clobber pending/running with a
        # stale read. No-op on SQLite (tests), real SELECT FOR UPDATE on Postgres.
        row = session.get(GoosecrackerSession, thread_id, with_for_update=True)
        if row is None:
            return None

        now = datetime.now(timezone.utc)
        row.updated_at = now

        if row.running and not _is_stale(row, now):
            # A live turn is in flight: this reply steers it rather than
            # queuing behind it (ADR 035 Phase 2). The steering insert and the
            # running-check above must be atomic under the row lock, so a
            # turn finishing concurrently can never be missed between the
            # check and the write. Do NOT append to the transcript here:
            # fetch_steering owns that write (with attribution) when the
            # running guest consumes the row, so appending it here too would
            # double it.
            session.add(
                GoosecrackerSteering(
                    thread_id=thread_id,
                    message_id=message_id,
                    author_id=author_id,
                    tier=tier or row.tier,
                    text=message,
                    delivered=False,
                )
            )
            session.commit()
            return {"action": "steering"}
        if row.running:
            # Stale/foreign-owned turn: the owner died or wedged. Reclaim its
            # in-flight work by folding it back into this dispatch so nothing
            # is lost (the startup sweep is the fast path; this is the inline
            # backstop when the process lived but the turn died silently).
            logger.warning(
                "goosecracker: reclaiming stale turn for thread %s "
                "(owner=%s, since=%s)",
                thread_id,
                row.runner_instance or "?",
                row.running_since,
            )
        # Idle (or reclaimed): the transcript gets this turn verbatim (the
        # dispatch path is the record of what was asked, unlike steering).
        row.transcript = _join_transcript(row.transcript, message)
        # Consume the dead turn's task (if any), the backlog, and this
        # message as a single task. Backlog + reclaimed message ids become
        # this turn's ack set (they show the reaction lifecycle); the
        # triggering message rides the live progress reply. Join non-empty
        # parts only: an empty pending/inflight must not inject a blank line
        # into the task.
        _task = "\n".join(p for p in (row.inflight_task, row.pending, message) if p)
        ack_ids = _merge_ids(row.inflight_ack_ids, row.pending_message_ids)
        _stored_recipe = row.recipe
        _stored_tier = row.tier
        _stored_repo = row.repo
        _stored_provider = row.provider
        row.pending = ""
        row.pending_message_ids = ""
        row.inflight_task = _task
        row.inflight_ack_ids = ack_ids
        row.running = True
        row.running_since = now
        row.runner_instance = INSTANCE_TOKEN
        session.add(row)
        session.commit()

    # Imports are lazy (circular import guard: chat.bot -> chat.goosecracker ->
    # goosecracker.runner -> chat.api).
    from goosecracker.api import submit

    result = submit(
        _task,
        session=thread_id,
        recipe=_stored_recipe,
        tier=_stored_tier,
        repo=_stored_repo,
        discord_thread=thread_id,
        provider=_stored_provider,
    )
    # Spread the submit result first, then set action: submit returns its own
    # "action" (create/resume) and a later key wins in a dict merge, so
    # "dispatched" must come last to override it (the bot routes on this).
    return {**result, "action": "dispatched"}


def enqueue_steering(
    thread_id: str, message_id: str, author_id: str, tier: str, text: str
) -> None:
    """Record a mid-run steering message for a running agent turn (ADR 035
    Phase 2). Inserted undelivered; the running guest recipe fetches and
    marks it delivered at its next stage boundary via ``fetch_steering``.

    A no-op on blank text so an empty reply never leaves a dangling row for
    the guest to chew on. Synchronous; call via ``asyncio.to_thread``.
    """
    text = text.strip()
    if not text:
        return
    with Session(get_engine()) as session:
        session.add(
            GoosecrackerSteering(
                thread_id=thread_id,
                message_id=message_id,
                author_id=author_id,
                tier=tier,
                text=text,
            )
        )
        session.commit()


def fetch_steering(thread_id: str, after_id: int = 0) -> list[dict]:
    """Deliver undelivered steering messages for a running agent turn.

    Locks and marks each undelivered row (``id > after_id``, ordered
    ascending) as delivered in a single read-modify-write, mirroring the
    ``with_for_update`` pattern in ``continue_session`` so a concurrent
    enqueue can never race a fetch into double-delivering (or losing) a row.
    Also appends each steered message to the session's curated transcript
    (with attribution) so the steerer's input survives in the record the same
    way an owner turn does; a missing session row (e.g. steering arrived after
    the thread was cleaned up) still delivers the rows, it just skips the
    transcript append rather than raising. Synchronous; call via
    ``asyncio.to_thread``.
    """
    with Session(get_engine()) as session:
        rows = session.exec(
            select(GoosecrackerSteering)
            .where(GoosecrackerSteering.thread_id == thread_id)
            .where(GoosecrackerSteering.delivered == False)  # noqa: E712 - SQL boolean
            .where(GoosecrackerSteering.id > after_id)
            .order_by(GoosecrackerSteering.id)
            .with_for_update()
        ).all()
        if not rows:
            return []

        session_row = session.get(GoosecrackerSession, thread_id, with_for_update=True)
        delivered: list[dict] = []
        for row in rows:
            # rows come from this session's own query, so they are already
            # attached; mutating attributes marks them dirty and commit()
            # flushes the update. No session.add() needed (and adding inside
            # a loop trips session-add-in-loop, which is for freshly
            # constructed rows, not already-tracked ones).
            row.delivered = True
            if session_row is not None:
                session_row.transcript = _join_transcript(
                    session_row.transcript,
                    f"[steering from {row.author_id}]: {row.text}",
                )
            delivered.append(
                {
                    "id": row.id,
                    "message_id": row.message_id,
                    "author_id": row.author_id,
                    "tier": row.tier,
                    "text": row.text,
                }
            )
        session.commit()
        return delivered


def drain_agent_queue(thread_id: str) -> tuple[str, list[str]] | None:
    """Drain a conversational agent thread's queue after a turn finishes.

    Called by the goosecracker runner (via ``chat.api``) when an agent turn
    completes. If replies were queued while the turn was running (``pending``
    non-empty), return ``(task, message_ids)`` as the next turn, promote them to
    the in-flight slot (``inflight_task``/``inflight_ack_ids``), re-own the turn
    under this process, and keep ``running=True`` so the caller dispatches the
    next turn. Otherwise fully idle the row (``running=False``, in-flight cleared)
    so the thread accepts new replies again, and return None.

    The row is locked FOR UPDATE for the whole read-modify-write so a message
    arriving concurrently in ``continue_session`` cannot clobber the drain (or be
    clobbered by it). Sync; the caller invokes it via ``asyncio.to_thread``. Lives
    here (not in the goosecracker package) so the runner reaches it through
    ``chat.api`` and never imports ``chat`` internals directly
    (import_boundaries_test).
    """
    with Session(get_engine()) as session:
        row = session.get(GoosecrackerSession, thread_id, with_for_update=True)
        if row is None:
            return None
        if row.runner_instance != INSTANCE_TOKEN:
            # Another process re-owned this turn (a reclaim after this replica lost
            # leadership without exiting): stop this now-demoted chain and let the
            # new owner drive the queue. Do NOT idle the row - the new owner is
            # running it. Returning None ends the caller's loop.
            logger.info(
                "goosecracker: drain aborted for %s; turn re-owned by another "
                "process (%s)",
                thread_id,
                row.runner_instance or "?",
            )
            return None
        if row.pending:
            task = row.pending
            ack_ids = _split_ids(row.pending_message_ids)
            # Promote the drained batch into the in-flight slot so a reclaim can
            # rebuild it and the runner's mark/ack reaction calls resolve it.
            row.inflight_task = task
            row.inflight_ack_ids = "\n".join(ack_ids)
            row.pending = ""
            row.pending_message_ids = ""
            row.running_since = datetime.now(timezone.utc)
            row.runner_instance = INSTANCE_TOKEN
            session.add(row)
            session.commit()
            return task, ack_ids
        row.running = False
        row.running_since = None
        row.inflight_task = ""
        row.inflight_ack_ids = ""
        session.add(row)
        session.commit()
        return None


def _owned_inflight_ack_ids(thread_id: str) -> list[str] | None:
    """This process's in-flight ack ids for ``thread_id``, or None if not owner.

    Returns None when the row is missing or has been re-owned by another process
    (leadership handed over), so callers can skip acting on a turn they no longer
    own. Returns [] when owned but nothing is queued.
    """
    with Session(get_engine()) as session:
        row = session.get(GoosecrackerSession, thread_id)
        if row is None or row.runner_instance != INSTANCE_TOKEN:
            return None
        return _split_ids(row.inflight_ack_ids)


def mark_inflight_running(thread_id: str) -> None:
    """Flip the running turn's queued messages ⏳ → 👀 (turn has started).

    Called by the runner at the start of each agent turn. Enqueues outbox reaction
    ops (leader-safe) rather than touching Discord directly, so it works from the
    runner's off-loop thread. A no-op when the turn has no queued acks (e.g. the
    first turn of a session, whose triggering message rides the live progress
    reply instead of a reaction) or when this process no longer owns the turn.
    Sync; call via ``asyncio.to_thread``.
    """
    ids = _owned_inflight_ack_ids(thread_id)
    if not ids:
        return
    from chat.outbox import enqueue_reaction

    with Session(get_engine()) as session:
        for msg_id in ids:
            enqueue_reaction(session, thread_id, msg_id, REACTION_QUEUED, remove=True)
            enqueue_reaction(session, thread_id, msg_id, REACTION_RUNNING)
        session.commit()


def force_idle_thread(thread_id: str) -> None:
    """Best-effort reset a thread to idle after a bookkeeping failure (self-heal).

    Called by the runner when the post-turn queue bookkeeping raised, so the
    thread does not wedge on ``running=True`` for the full stale timeout. CAS on
    ownership: only idles the row if this process still owns it, so it never
    clobbers a turn another process has already re-owned. Sync; call via
    ``asyncio.to_thread``.
    """
    with Session(get_engine()) as session:
        row = session.get(GoosecrackerSession, thread_id, with_for_update=True)
        if row is None or row.runner_instance != INSTANCE_TOKEN:
            return
        row.running = False
        row.running_since = None
        row.inflight_task = ""
        row.inflight_ack_ids = ""
        session.add(row)
        session.commit()


def ack_inflight(thread_id: str, success: bool) -> None:
    """Resolve the running turn's queued messages 👀 → ✅ (or ❌), and clear the
    in-flight slot.

    Called by the runner when an agent turn finishes (before it drains the next
    batch). Removes the transient ⏳/👀 markers and adds the terminal reaction on
    each acked message, then clears ``inflight_task``/``inflight_ack_ids`` (that
    batch is done; the next drain repopulates them). Sync; call via
    ``asyncio.to_thread``.
    """
    from chat.outbox import enqueue_reaction

    terminal = REACTION_DONE if success else REACTION_FAILED
    with Session(get_engine()) as session:
        row = session.get(GoosecrackerSession, thread_id, with_for_update=True)
        if row is None or row.runner_instance != INSTANCE_TOKEN:
            # Not our turn anymore (re-owned after a leadership handover): the new
            # owner will resolve these reactions. Skip rather than double-post.
            return
        for msg_id in _split_ids(row.inflight_ack_ids):
            enqueue_reaction(session, thread_id, msg_id, REACTION_QUEUED, remove=True)
            enqueue_reaction(session, thread_id, msg_id, REACTION_RUNNING, remove=True)
            enqueue_reaction(session, thread_id, msg_id, terminal)
        row.inflight_task = ""
        row.inflight_ack_ids = ""
        session.add(row)
        session.commit()


def reclaim_orphaned_agent_sessions() -> int:
    """Re-dispatch agent turns orphaned by a dead owner (leader restart / crash).

    Every running turn stamps its session with this process's ``INSTANCE_TOKEN``.
    A running agent session whose token differs was owned by a process that no
    longer exists (in-process daemon threads do not survive a restart), so its
    turn is dead and must be reclaimed or the thread wedges forever with queued
    replies stuck on ⏳. Rebuilds the next task losslessly from ``inflight_task``
    + ``pending``, re-owns it under this process, and re-dispatches (durably, on
    the leader's long-lived loop). Returns the number reclaimed.

    Invoked once from ``_start_singletons`` on leader acquisition. Sync; call via
    ``asyncio.to_thread``.
    """
    now = datetime.now(timezone.utc)
    with Session(get_engine()) as session:
        candidates = session.exec(
            select(GoosecrackerSession.discord_thread)
            .where(GoosecrackerSession.running == True)  # noqa: E712 - SQL boolean
            .where(GoosecrackerSession.recipe == AGENT_RECIPE)
            .where(GoosecrackerSession.runner_instance != INSTANCE_TOKEN)
        ).all()

    reclaimed = 0
    for thread_id in candidates:
        dispatch = _reclaim_one(thread_id, now)
        if dispatch is None:
            continue
        task, recipe, tier, repo = dispatch
        from goosecracker.api import submit

        submit(
            task,
            session=thread_id,
            recipe=recipe,
            tier=tier,
            repo=repo,
            discord_thread=thread_id,
        )
        reclaimed += 1
    if reclaimed:
        logger.warning(
            "goosecracker: reclaimed %d orphaned agent turn(s) on startup", reclaimed
        )
    return reclaimed


def _reclaim_one(thread_id: str, now: datetime) -> tuple[str, str, str, str] | None:
    """Re-own one orphaned session and return its dispatch params, or None.

    Locks the row, re-checks it is still a foreign-owned running turn (another
    replica may have raced), rebuilds the task from inflight + pending, and stamps
    it under this process. Returns None (and idles the row) when there is nothing
    to run, so the sweep never dispatches an empty turn."""
    with Session(get_engine()) as session:
        row = session.get(GoosecrackerSession, thread_id, with_for_update=True)
        if row is None or not row.running or row.recipe != AGENT_RECIPE:
            return None
        if row.runner_instance == INSTANCE_TOKEN:
            return None  # this process already owns it; still alive
        task = "\n".join(p for p in (row.inflight_task, row.pending) if p)
        ack_ids = _merge_ids(row.inflight_ack_ids, row.pending_message_ids)
        recipe, tier, repo = row.recipe, row.tier, row.repo
        row.pending = ""
        row.pending_message_ids = ""
        if not task:
            # Nothing recoverable to run: idle the thread so new replies flow.
            row.running = False
            row.running_since = None
            row.inflight_task = ""
            row.inflight_ack_ids = ""
            session.add(row)
            session.commit()
            return None
        row.inflight_task = task
        row.inflight_ack_ids = ack_ids
        row.running_since = now
        row.runner_instance = INSTANCE_TOKEN
        session.add(row)
        session.commit()
        return task, recipe, tier, repo


def start_agent_session(
    thread_id: str,
    repo: str,
    prompt: str,
    parent_channel_id: str = "",
    *,
    task: str = "",
    plan: "orchestrator_plan.Plan | None" = None,
) -> dict:
    """Open a conversational agent session for a Discord thread.

    Dispatches the first turn and writes a GoosecrackerSession row so that
    follow-up replies in the thread are routed through ``continue_session``
    with queuing support. The row is written AFTER dispatch so that a failed
    submit leaves no orphan row.

    ``parent_channel_id`` is the channel the /agent command was run from (the
    thread's parent); stored on the row so the runner can fetch channel-scoped
    context for a conversational reply, since the new thread has no history.

    ``task`` is what the guest actually executes for this first turn. It defaults
    to ``prompt`` (today's behaviour). ADR 036 passes the compiled brief markdown
    plus the raw prompt here, while ``transcript`` keeps the raw prompt so the
    user's words stay the ground truth for follow-up turns. ``inflight_task``
    tracks the submitted task so a reclaim re-dispatches the same work.

    ``plan`` is the optional runtime :class:`~chat.orchestrator_plan.Plan` from
    the DeepSeek orchestrator (runtime-recipes plan, Task 6): when set it is
    threaded through to ``goosecracker.dispatch.submit`` so the first turn
    delivers a rendered router + plan file via ``injectedContext`` instead of
    the baked ``recipe="agent"`` path. Absent, behavior is unchanged.

    Synchronous; call via asyncio.to_thread.
    """
    prompt = prompt.strip()
    task = (task or prompt).strip()
    # Imported lazily to avoid a circular import (chat.bot ->
    # chat.goosecracker -> goosecracker.runner -> chat.api).
    from goosecracker.api import submit

    # Dispatch first: a failed submit leaves no session row, avoiding a stuck
    # thread where future replies queue forever with no runner to drain them.
    result = submit(
        task,
        session=thread_id,
        recipe=AGENT_RECIPE,
        tier=AGENT_TIER,
        repo=repo,
        discord_thread=thread_id,
        plan=plan,
    )
    with Session(get_engine()) as session:
        session.add(
            GoosecrackerSession(
                discord_thread=thread_id,
                parent_channel_id=parent_channel_id,
                recipe=AGENT_RECIPE,
                tier=AGENT_TIER,
                repo=repo,
                transcript=prompt,
                running=True,
                # Stamp the in-flight turn so a reply arriving during it queues
                # (not "stale"), and a reclaim can rebuild it losslessly.
                inflight_task=task,
                running_since=datetime.now(timezone.utc),
                runner_instance=INSTANCE_TOKEN,
            )
        )
        session.commit()
    return result


def is_goosecracker_thread(thread_id: str) -> bool:
    """Whether a Discord thread id has a goosecracker session. Synchronous."""
    with Session(get_engine()) as session:
        return session.get(GoosecrackerSession, thread_id) is not None


def session_scope(thread_id: str) -> str | None:
    """Return the repo scope stored for a goosecracker thread, or None.

    Used by the bot's ACL gate on agent-thread replies (ADR 035 Phase 2) to look
    up which repo an ``is_granted`` check should be scoped to. None when the
    thread has no session row; "" when it does but is a repo-less run.
    Synchronous; call via ``asyncio.to_thread``.
    """
    with Session(get_engine()) as session:
        row = session.get(GoosecrackerSession, thread_id)
        return row.repo if row else None


def artifact_id_for_thread(thread_id: str) -> str:
    """Return the thread's unguessable capability artifact id, assigning one on
    first use (ADR 024 amendment). Synchronous; call via ``asyncio.to_thread``.

    Random per thread and stored on the session row so a re-publish reuses it and
    the live page hot-reloads at a stable URL, while the URL is NOT the enumerable
    Discord thread id, so it is not publicly discoverable but is safe to share by
    link. Falls back to a fresh random id if the thread has no session row.
    """
    with Session(get_engine()) as session:
        row = session.get(GoosecrackerSession, thread_id)
        if row is None:
            return secrets.token_urlsafe(9)
        if not row.artifact_id:
            row.artifact_id = secrets.token_urlsafe(9)
            session.add(row)
            session.commit()
        return row.artifact_id


def ensure_steering_token(thread_id: str) -> str:
    """Return the thread's unguessable steering capability token, assigning one
    on first use (ADR 035 Phase 2 hardening). Synchronous; call via
    ``asyncio.to_thread``.

    Mirrors ``artifact_id_for_thread``: random per thread, stored on the session
    row so a re-dispatch reuses the same token, and idempotent (a second call
    returns the existing value rather than rotating it). Returns "" when the
    thread has no session row (nothing to bind a token to).
    """
    with Session(get_engine()) as session:
        row = session.get(GoosecrackerSession, thread_id, with_for_update=True)
        if row is None:
            return ""
        if not row.steering_token:
            row.steering_token = secrets.token_urlsafe(24)
            session.add(row)
            session.commit()
        return row.steering_token


def thread_for_steering_token(token: str) -> str | None:
    """Resolve a steering token back to its owning thread id, or None.

    The steering endpoint is keyed on this token (not the guessable Discord
    thread snowflake), so a compromised guest can only ever resolve its own
    thread's steering. None on an empty or unknown token. Synchronous; call via
    ``asyncio.to_thread``.
    """
    if not token:
        return None
    with Session(get_engine()) as session:
        row = session.exec(
            select(GoosecrackerSession).where(
                GoosecrackerSession.steering_token == token
            )
        ).first()
        return row.discord_thread if row else None


def parent_channel_for_thread(thread_id: str) -> str:
    """Return the Discord parent channel id stored for an agent thread, or "".

    The runner reads this at delivery time to fetch channel-scoped context for a
    conversational reply. Empty when the thread has no session row (an
    MCP-dispatched run with no Discord front) or none was recorded (an older
    thread predating this column, or an artifact thread). Sync; call via
    ``asyncio.to_thread``.
    """
    with Session(get_engine()) as session:
        row = session.get(GoosecrackerSession, thread_id)
        return row.parent_channel_id if row else ""


# Caller-provided context injection (ADR 040). Bounds on the transcript packed
# into the guest: a message count cap (recent history, not the full channel
# archive) and a per-message character cap (one runaway paste should not blow
# the budget for the rest of the turn).
_INJECTED_CONTEXT_MSG_LIMIT = 50
_INJECTED_CONTEXT_PER_MSG_CHARS = 2000
# Fallback path (own-transcript): keep the most recent slice so a long-running
# thread's context stays bounded, mirroring the channel path's overall budget.
_INJECTED_CONTEXT_OWN_CHARS = 8000


def build_injected_context(thread_id: str, tier: str = "") -> dict[str, str]:
    """Build the caller-provided context bundle for an agent turn (ADR 040).

    Returns a ``{filename: content}`` map staged into the guest's
    ``/injected-context/``. Source-aware (Discord): resolves the thread's parent
    channel and packs its recent messages as ``transcript.md`` plus a
    self-describing ``README.md``. Empty map when the thread has no parent
    channel or the channel has no messages, so callers can inject
    unconditionally. Sync; call via ``asyncio.to_thread``. ``tier`` is accepted
    for the trust-tier filter (ADR 040 Security); all current tiers may see the
    invoking channel.
    """
    parent = parent_channel_for_thread(thread_id)
    if parent:
        with Session(get_engine()) as session:
            rows = session.exec(
                select(Message)
                .where(Message.channel_id == parent)
                .order_by(Message.created_at.desc())
                .limit(_INJECTED_CONTEXT_MSG_LIMIT)
            ).all()
        if rows:
            return _context_from_channel(list(reversed(rows)), parent)
    # The parent-channel path resolved nothing: either no parent channel is
    # recorded on the session row (an MCP-dispatched run, an id space that does
    # not key a GoosecrackerSession, or an older thread) or the parent channel
    # has no messages. Fall back to the thread's OWN accumulated transcript so a
    # "do it again" / "change it" follow-up still carries this thread's earlier
    # turns instead of shipping an empty /injected-context/ and costing the owner
    # a correction turn.
    own = _own_thread_transcript(thread_id)
    if own:
        if parent:
            # A parent channel WAS recorded but resolved no messages: the primary
            # context path failed unexpectedly, so warn to make it visible.
            logger.warning(
                "build_injected_context: parent channel %s for thread %s had no "
                "messages; falling back to the thread's own transcript (%d chars)",
                parent,
                thread_id,
                len(own),
            )
        else:
            # No parent channel recorded (an MCP-dispatched thread, or an older
            # thread predating the column): the own-transcript fallback is the
            # normal path here, not an anomaly, so log at info.
            logger.info(
                "build_injected_context: no parent channel for thread %s; using "
                "the thread's own transcript (%d chars)",
                thread_id,
                len(own),
            )
        return _context_from_own_transcript(own)
    # Truly nothing to inject: a brand-new thread with no prior turns. Expected
    # on a first turn, so nothing to warn about.
    return {}


def _context_from_channel(rows: list[Message], parent: str) -> dict[str, str]:
    """Build the injected-context bundle from a parent channel's recent messages."""
    lines = []
    truncated = 0
    for m in rows:
        body = m.content or ""
        if len(body) > _INJECTED_CONTEXT_PER_MSG_CHARS:
            body = body[:_INJECTED_CONTEXT_PER_MSG_CHARS] + " [truncated]"
            truncated += 1
        lines.append(f"{m.username}: {body}")
    if truncated:
        logger.info(
            "build_injected_context: truncated %d/%d messages for channel %s",
            truncated,
            len(rows),
            parent,
        )
    transcript = "\n".join(lines)
    readme = (
        "# Injected context\n\n"
        "This directory (`/injected-context/`) holds context the caller staged "
        "for this task. You did not gather it and it is not in the repo. Grep or "
        "read it when the user refers to an earlier discussion.\n\n"
        f"- Source: recent messages from Discord channel `{parent}` "
        "(the parent of this agent thread).\n"
        f"- `transcript.md`: the last {len(rows)} message(s), oldest first, "
        "as of this turn. Rebuilt every turn, so it grows as the thread advances.\n"
    )
    return {"README.md": readme, "transcript.md": transcript}


def _own_thread_transcript(thread_id: str) -> str:
    """The thread's own accumulated prompt transcript (its prior turns), or "".

    ``GoosecrackerSession.transcript`` records the user's prompts for this
    thread, one turn per blank-line-separated block. Sync; call via
    ``asyncio.to_thread``.
    """
    with Session(get_engine()) as session:
        row = session.get(GoosecrackerSession, thread_id)
        return (row.transcript or "").strip() if row else ""


def _context_from_own_transcript(transcript: str) -> dict[str, str]:
    """Build the injected-context bundle from the thread's own prior turns.

    Used when the parent-channel path is unavailable. Keeps the most recent
    slice so a long thread stays within budget.
    """
    body = transcript
    if len(body) > _INJECTED_CONTEXT_OWN_CHARS:
        body = "[...earlier turns omitted...]\n" + body[-_INJECTED_CONTEXT_OWN_CHARS:]
    readme = (
        "# Injected context\n\n"
        "This directory (`/injected-context/`) holds context the caller staged "
        "for this task. You did not gather it and it is not in the repo. Grep or "
        "read it when the user refers to an earlier discussion.\n\n"
        "- Source: this agent thread's own prior turns (the parent Discord "
        "channel was unavailable this turn).\n"
        "- `transcript.md`: the prompts sent to this thread so far, oldest "
        'first. Use it to resolve a follow-up like "do it again" or "change '
        'it" against what the thread was already working on.\n'
    )
    return {"README.md": readme, "transcript.md": body}


async def build_roast(attempt_text: str) -> str:
    """Roast a non-owner who tried to run the agent, via the in-cluster qwen
    model (same path as the changelog roasts). Falls back to a fixed line if the
    model is unavailable, so the gate always replies.
    """
    from chat.summarizer import build_llm_caller

    attempt_text = (attempt_text or "").strip()[:300]
    prompt = (
        "You are a cynical senior engineer. Someone who is NOT granted just "
        "tried to steer a coding-agent thread that isn't theirs to drive"
        + (f' with: "{attempt_text}"' if attempt_text else "")
        + ". Roast them in one or two dry sentences for reaching for a tool that "
        "isn't theirs. Past tense or present, declarative. No preamble, no "
        "markdown, no emoji, no hedging."
    )
    try:
        call_llm = build_llm_caller()
        roast = (await call_llm(prompt)).strip()
        return roast or _FALLBACK_ROAST
    except Exception:
        logger.exception("goosecracker: roast generation failed")
        return _FALLBACK_ROAST
