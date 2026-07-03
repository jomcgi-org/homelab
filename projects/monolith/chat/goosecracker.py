"""goosecracker: the owner-gated Discord artifact agent (ADR 024 Task 4).

``/artifact <prompt>`` opens a Discord thread and runs goose in a
Firecracker microVM (the ``artifact`` recipe + tier), which builds a
self-contained HTML artifact and publishes it; fc-agentd posts the artifact URL
back into the thread (Task 5). Each owner follow-up in the thread re-runs goose
from scratch with the FULL accumulated transcript (Model B), re-publishing the
same artifact id so the live page hot-reloads.

This module is the pure logic seam (gate + transcript + dispatch + roast) so the
Discord wiring in ``chat.bot`` stays thin and this stays unit-testable. The DB
helpers are synchronous and open their own session, so the bot calls them via
``asyncio.to_thread`` (never blocking the gateway loop).
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app.db import get_engine
from chat.models import GoosecrackerSession

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

# The artifact recipe + model tier (ADR 024). The tier selects Gemini via
# OpenRouter (key swapped at egress) and bounds the secret placeholders the guest
# holds; the recipe is the write-only artifact builder.
ARTIFACT_RECIPE = "artifact"
ARTIFACT_TIER = "artifact"

# The agent recipe + tier: default (empty) tier runs on the in-cluster Qwen model.
# The agent recipe is the general coding agent (goosecracker general mode).
AGENT_RECIPE = "agent"
AGENT_TIER = ""

# Shown when the owner gate rejects someone and the qwen roast path is
# unavailable (model down), so a non-owner always gets a clear refusal.
_FALLBACK_ROAST = "Nice try. /artifact is owner-only."


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


def start_session(thread_id: str, prompt: str) -> dict:
    """Record a new thread's transcript and dispatch the first artifact run.

    Synchronous (opens its own session); call via ``asyncio.to_thread``. Returns
    the dispatch result (``thread_id`` + ``action``).
    """
    prompt = prompt.strip()
    # Imported lazily (not at module load): chat.bot imports this module and
    # goosecracker.runner imports chat.api, so a module-level import would risk a
    # circular import. goosecracker.api is the boundary-approved surface.
    from goosecracker.api import submit

    # Dispatch first: if submit raises, no session row is left behind (which
    # would otherwise make later replies look like a live goosecracker thread
    # with no backing run). The Discord thread id doubles as the run session so
    # the guest's progress stream and the bot's poll key line up.
    result = submit(
        prompt,
        session=thread_id,
        recipe=ARTIFACT_RECIPE,
        tier=ARTIFACT_TIER,
        discord_thread=thread_id,
    )
    with Session(get_engine()) as session:
        session.add(GoosecrackerSession(discord_thread=thread_id, transcript=prompt))
        session.commit()
    return result


def continue_session(thread_id: str, message: str, message_id: str = "") -> dict | None:
    """Append an owner follow-up and re-dispatch or queue depending on recipe.

    Artifact sessions (recipe="artifact"): re-dispatch the full transcript (Model
    B), preserving the original ADR 026 Phase 2 resume gate. Returns the raw
    submit result dict.

    Agent sessions (recipe="agent"): conversational queuing. If a turn is already
    running (and not stale), append the message to ``pending`` + its id to
    ``pending_message_ids`` and return ``{"action": "queued"}`` so the bot can
    acknowledge with a ⏳ reaction instead of a text reply. When idle (or the prior
    turn is stale and reclaimed), build the next task from any backlog + this
    message, set running=True, dispatch, and return
    ``{"action": "dispatched", ...submit result}``.

    Returns None when ``thread_id`` is not a goosecracker thread so the caller
    can fall through to normal chat handling. Synchronous; call via
    asyncio.to_thread.
    """
    message = message.strip()
    # Captured inside the session for use after the with-block.
    _is_agent = False
    _task = ""
    _stored_recipe = ""
    _stored_tier = ""
    _stored_repo = ""
    _transcript = ""

    with Session(get_engine()) as session:
        # Lock the row for the whole read-modify-write so a concurrent
        # drain_agent_queue (runner thread) cannot clobber pending/running with a
        # stale read. No-op on SQLite (tests), real SELECT FOR UPDATE on Postgres.
        row = session.get(GoosecrackerSession, thread_id, with_for_update=True)
        if row is None:
            return None

        # Accumulate the turn in the curated transcript for all recipe types.
        transcript = _join_transcript(row.transcript, message)
        row.transcript = transcript
        now = datetime.now(timezone.utc)
        row.updated_at = now

        if row.recipe == AGENT_RECIPE:
            _is_agent = True
            if row.running and not _is_stale(row, now):
                # A live turn is in flight: queue this reply for the next turn and
                # record its id so the runner can react ⏳→👀→✅ on this message.
                row.pending = _append_pending(row.pending, message)
                row.pending_message_ids = _append_id(
                    row.pending_message_ids, message_id
                )
                session.add(row)
                session.commit()
                return {"action": "queued"}
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
            # Idle (or reclaimed): consume the dead turn's task (if any), the
            # backlog, and this message as a single task. Backlog + reclaimed
            # message ids become this turn's ack set (they show the reaction
            # lifecycle); the triggering message rides the live progress reply.
            # Join non-empty parts only: an empty pending/inflight must not inject
            # a blank line into the task.
            _task = "\n".join(p for p in (row.inflight_task, row.pending, message) if p)
            ack_ids = _merge_ids(row.inflight_ack_ids, row.pending_message_ids)
            _stored_recipe = row.recipe
            _stored_tier = row.tier
            _stored_repo = row.repo
            row.pending = ""
            row.pending_message_ids = ""
            row.inflight_task = _task
            row.inflight_ack_ids = ack_ids
            row.running = True
            row.running_since = now
            row.runner_instance = INSTANCE_TOKEN
            session.add(row)
            session.commit()
        else:
            # Artifact path: commit the transcript update, then do the S3 check.
            _transcript = transcript
            session.add(row)
            session.commit()

    # Imports are lazy (circular import guard: chat.bot -> chat.goosecracker ->
    # goosecracker.runner -> chat.api).
    from goosecracker.api import submit

    if _is_agent:
        result = submit(
            _task,
            session=thread_id,
            recipe=_stored_recipe,
            tier=_stored_tier,
            repo=_stored_repo,
            discord_thread=thread_id,
        )
        # Spread the submit result first, then set action: submit returns its own
        # "action" (create/resume) and a later key wins in a dict merge, so
        # "dispatched" must come last to override it (the bot routes on this).
        return {**result, "action": "dispatched"}

    # Artifact path: ADR 026 Phase 2 (Model A vs B). If a persisted goose session
    # exists for this thread, send only the new reply (Model A) so the guest can
    # restore the session and edit in place; otherwise cold-rebuild from the full
    # transcript (Model B). The S3 check is best-effort: a failure falls back to
    # Model B, never a hard error.
    from artifact import s3

    has_session = False
    try:
        has_session = s3.head_session(thread_id) is not None
    except Exception:
        logger.exception("goosecracker: session existence check failed; cold rebuild")
    task = message if has_session else _transcript
    return submit(
        task,
        session=thread_id,
        recipe=ARTIFACT_RECIPE,
        tier=ARTIFACT_TIER,
        discord_thread=thread_id,
    )


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
    thread_id: str, repo: str, prompt: str, parent_channel_id: str = ""
) -> dict:
    """Open a conversational agent session for a Discord thread.

    Dispatches the first turn and writes a GoosecrackerSession row so that
    follow-up replies in the thread are routed through ``continue_session``
    (agent path) with queuing support. The row is written AFTER dispatch so
    that a failed submit leaves no row (mirroring ``start_session``).

    ``parent_channel_id`` is the channel the /agent command was run from (the
    thread's parent); stored on the row so the runner can fetch channel-scoped
    context for a conversational reply, since the new thread has no history.

    Synchronous; call via asyncio.to_thread.
    """
    prompt = prompt.strip()
    # Imported lazily for the same reason as start_session (circular import
    # guard: chat.bot -> chat.goosecracker -> goosecracker.runner -> chat.api).
    from goosecracker.api import submit

    # Dispatch first: a failed submit leaves no session row, avoiding a stuck
    # thread where future replies queue forever with no runner to drain them.
    result = submit(
        prompt,
        session=thread_id,
        recipe=AGENT_RECIPE,
        tier=AGENT_TIER,
        repo=repo,
        discord_thread=thread_id,
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
                inflight_task=prompt,
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


def is_agent_thread(thread_id: str) -> bool:
    """True for an agent (not artifact) goosecracker thread. Synchronous.

    Lets the reply gate open agent threads to everyone while keeping artifact
    threads owner-only (ADR 029).
    """
    with Session(get_engine()) as session:
        row = session.get(GoosecrackerSession, thread_id)
        return row is not None and row.recipe == AGENT_RECIPE


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


async def build_roast(attempt_text: str) -> str:
    """Roast a non-owner who tried to run the agent, via the in-cluster qwen
    model (same path as the changelog roasts). Falls back to a fixed line if the
    model is unavailable, so the gate always replies.
    """
    from chat.summarizer import build_llm_caller

    attempt_text = (attempt_text or "").strip()[:300]
    prompt = (
        "You are a cynical senior engineer. Someone who is NOT the owner just "
        "tried to run the owner-only /artifact command"
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
