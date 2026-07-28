"""WhatsApp group agent sessions (ADR 039 Phase 4, spec sections 4 and 6).

WhatsApp has no threads, so a group's agent session is keyed on the group JID.
This module is the WhatsApp-shaped counterpart to the Discord session logic in
``chat.goosecracker``: it routes participant messages during a run to steering
(with author attribution), and drives the live checklist through
``chat.whatsapp_outbox`` edit rows (with the ~15-minute edit-window repost).

Every WhatsApp outbound action goes through ``chat.whatsapp_outbox`` (the Go
gateway is the only sender), so the reaction lifecycle, the checklist, and the
final result are all enqueued as outbox rows here rather than posted to Discord.
The Discord path in ``chat.goosecracker`` / ``goosecracker.runner`` is unchanged;
these two providers meet only at the ``provider`` discriminator on the session row.

Session key: a group JID (``12345-67890@g.us``) contains ':', '@' and '.', which
the internal progress and steering endpoints reject (their id guard is
``^[A-Za-z0-9_-]{1,64}$``). So the session id stored in the ``discord_thread`` PK
is a sanitized ``wa-<group_jid>`` key; the raw JID is stored in
``provider_group_jid`` for the outbox writers. The key is derived from the group,
so one row per group (one active session per group) falls out of the PK.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

from sqlmodel import Session, select

from core.db import get_engine
from chat.models import (
    GoosecrackerSession,
    GoosecrackerSteering,
    WhatsappGroup,
    WhatsappOutbox,
)
from chat.whatsapp_outbox import (
    enqueue_edit,
    enqueue_message,
    enqueue_message_returning_id,
    enqueue_reaction,
)
from goosecracker.api import tier_allows

logger = logging.getLogger(__name__)

# The household tier for WhatsApp group sessions (ADR 039, amended). Granted every
# LOCAL capability (knowledge, calendar, reminders, artifact/chart builds); only
# repo and cluster stay denied, being the credentialed families the partner-phone
# guest must not hold (goosecracker.tiers).
HOUSEHOLD_TIER = "household"

# Sanitized-key prefix. Discord thread ids are numeric snowflakes, so a
# "wa-"-prefixed id never collides with a Discord session id.
_WA_KEY_PREFIX = "wa-"

def wa_session_key(group_jid: str) -> str:
    """Return the sanitized, id-guard-safe session key for a group JID.

    The internal progress/steering endpoints validate the id against
    ``^[A-Za-z0-9_-]{1,64}$``; a raw JID (``...@g.us``) fails that, so ':' '@' '.'
    are folded to '-'. Real JIDs contain exactly one '@' and a '.' only inside the
    domain, so this fold is injective across realistic JIDs (a single household
    group in v1). Truncated to fit the 64-char guard.
    """
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", group_jid)
    return f"{_WA_KEY_PREFIX}{safe}"[:64]


def is_whatsapp_session_key(session_key: str) -> bool:
    """Whether ``session_key`` addresses a WhatsApp group session (cheap, no DB)."""
    return session_key.startswith(_WA_KEY_PREFIX)


def household_allows(feature: str) -> bool:
    """Whether the household tier may use ``feature`` (repo/cluster/artifact are
    denied; knowledge/calendar/reminders granted). Thin wrapper over the shared
    tier ACL so callers do not hardcode the tier name."""
    return tier_allows(HOUSEHOLD_TIER, feature)


# --- Steering (participant messages during a run) ---------------------------


def steer_or_none(
    session_key: str,
    *,
    message_id: str,
    sender_jid: str,
    sender_name: str,
    text: str,
) -> bool:
    """Route an engaged message to steering when the group's session is running.

    Returns True when the message was recorded as steering (the group has a live,
    non-stale run), so the caller does NOT start a new task; False when there is
    no live run, so the caller falls through to dispatch/chat. The running check
    and the steering insert happen atomically under the session row lock (mirrors
    ``chat.goosecracker.continue_session``) so a turn finishing concurrently
    cannot slip a steering row in after the guest has stopped polling.

    Attribution carries both the sender JID (``author_id``, the stable identity)
    and the WhatsApp push name (``author_name``, readable), so the running guest's
    stage-boundary poll and the transcript record who steered. A 👀 reaction on
    the steering message acknowledges it was heard without a text reply.

    Synchronous; call via ``asyncio.to_thread``.
    """
    text = text.strip()
    if not text:
        return False
    from chat.goosecracker import REACTION_RUNNING, _is_stale

    with Session(get_engine()) as session:
        row = session.get(GoosecrackerSession, session_key, with_for_update=True)
        if row is None:
            return False
        now = datetime.now(timezone.utc)
        if not (row.running and not _is_stale(row, now)):
            return False
        session.add(
            GoosecrackerSteering(
                thread_id=session_key,
                message_id=message_id,
                author_id=sender_jid,
                author_name=sender_name,
                tier=row.tier,
                text=text,
                delivered=False,
            )
        )
        enqueue_reaction(
            session, row.provider_group_jid, message_id, sender_jid, REACTION_RUNNING
        )
        session.commit()
    return True


# --- Reaction lifecycle on the trigger message ------------------------------
#
# The ⏳ → 👀 → ✅ lifecycle for a WhatsApp session runs on the single triggering
# message (spec section 4), unlike the Discord path which reacts on each queued
# message. The reaction build needs the trigger's sender JID, which the session
# row carries. These are called from ``chat.goosecracker.mark_inflight_running`` /
# ``ack_inflight`` under a provider branch, with the row already loaded and its
# ownership already checked, so they only enqueue the reaction ops.


def emit_running_reaction(session: Session, row: GoosecrackerSession) -> None:
    """⏳ → 👀 on the trigger message (turn started)."""
    from chat.goosecracker import REACTION_QUEUED, REACTION_RUNNING

    # Both fields are required to build a reaction (enqueue_reaction raises without
    # the sender JID); a missing either means skip, so a partial trigger cannot
    # wedge the run with running=True via an unguarded ValueError.
    if not row.provider_trigger_message_id or not row.provider_trigger_sender_jid:
        return
    enqueue_reaction(
        session,
        row.provider_group_jid,
        row.provider_trigger_message_id,
        row.provider_trigger_sender_jid,
        REACTION_QUEUED,
        remove=True,
    )
    enqueue_reaction(
        session,
        row.provider_group_jid,
        row.provider_trigger_message_id,
        row.provider_trigger_sender_jid,
        REACTION_RUNNING,
    )


def emit_terminal_reaction(
    session: Session, row: GoosecrackerSession, success: bool
) -> None:
    """👀 → ✅ (or ❌) on the trigger message (turn finished)."""
    from chat.goosecracker import (
        REACTION_DONE,
        REACTION_FAILED,
        REACTION_QUEUED,
        REACTION_RUNNING,
    )

    # Both fields required (see emit_running_reaction): skip if either is missing.
    if not row.provider_trigger_message_id or not row.provider_trigger_sender_jid:
        return
    terminal = REACTION_DONE if success else REACTION_FAILED
    for prior in (REACTION_QUEUED, REACTION_RUNNING):
        enqueue_reaction(
            session,
            row.provider_group_jid,
            row.provider_trigger_message_id,
            row.provider_trigger_sender_jid,
            prior,
            remove=True,
        )
    enqueue_reaction(
        session,
        row.provider_group_jid,
        row.provider_trigger_message_id,
        row.provider_trigger_sender_jid,
        terminal,
    )


# --- Live checklist through outbox edit rows --------------------------------
#
# The checklist is a single bot message (posted at dispatch) edited in place at
# stage boundaries. Edits are coalesced per session on the same in-memory gate the
# Discord stream uses (_should_edit_checklist), keyed here by session id. When the
# gateway reports the ~15-minute edit window closed (last_error='edit_window_
# expired' on the edit row), a fresh checklist message is posted and the session's
# checklist_outbox_id is repointed to it, so editing continues on the new message.

# Per-session coalescing state: session_key -> (stages_version, done, monotonic).
_last_checklist_edit: dict[str, tuple[int, bool, float]] = {}


def checklist_on_progress(session_key: str) -> None:
    """Emit a coalesced checklist edit for a WhatsApp run, if one is due.

    Called from the progress sink for every stdout chunk of a ``wa-`` session, so
    it must be cheap when nothing changed: it renders the checklist from the
    in-memory progress buffer and applies the same stages_version/done +
    min-interval gate as the Discord stream before touching the DB. Only when an
    edit is actually due does it enqueue an outbox ``edit`` row (reposting first if
    the edit window has closed). Synchronous; call via ``asyncio.to_thread``.
    """
    checklist = _render(session_key)
    if checklist is None:
        return
    snap = _progress_get(session_key)
    if snap is None:
        return
    last_version, last_done, last_at = _last_checklist_edit.get(
        session_key, (-1, False, 0.0)
    )
    now = time.monotonic()
    if not _should_edit(
        snap.stages_version, last_version, snap.done, last_done, now, last_at
    ):
        return
    if _emit_checklist_edit(session_key, checklist):
        _last_checklist_edit[session_key] = (snap.stages_version, snap.done, now)


def checklist_final(session_key: str) -> None:
    """Force the terminal checklist edit at run end, bypassing the coalescer.

    The runner marks the progress buffer done in-process (not via the HTTP sink),
    so the coalesced ``checklist_on_progress`` path may not see the final
    transition. Called once when the turn completes to render the resolved
    checklist (all stages done/skipped) and drop the per-session coalescing state.
    A run that never announced a stage plan renders no checklist, so the planning
    message is left as-is (the result message carries the answer). Synchronous;
    call via ``asyncio.to_thread``.
    """
    checklist = _render(session_key)
    if checklist is not None:
        _emit_checklist_edit(session_key, checklist)
    _last_checklist_edit.pop(session_key, None)


def _emit_checklist_edit(session_key: str, content: str) -> bool:
    """Enqueue an edit of the session's checklist message, reposting on expiry.

    Returns True when a row was enqueued (edit or repost), False when there is no
    session/checklist to edit. When a prior edit for the current checklist message
    was consumed as ``edit_window_expired``, a fresh checklist message is posted
    and ``checklist_outbox_id`` is repointed to it (the fresh message already
    carries ``content``, so no edit is enqueued this round).
    """
    with Session(get_engine()) as session:
        row = session.get(GoosecrackerSession, session_key, with_for_update=True)
        if row is None or not row.checklist_outbox_id:
            return False
        group_jid = row.provider_group_jid
        checklist_id = row.checklist_outbox_id
        if _edit_window_expired(session, checklist_id):
            new_id = enqueue_message_returning_id(session, group_jid, content=content)
            row.checklist_outbox_id = new_id
            session.add(row)
            session.commit()
            logger.info(
                "whatsapp: checklist edit window closed for %s; reposted as %d",
                session_key,
                new_id,
            )
            return True
        enqueue_edit(session, group_jid, checklist_id, content)
        session.commit()
        return True


def _edit_window_expired(session: Session, checklist_id: int) -> bool:
    """Whether an edit of ``checklist_id`` was consumed as window-expired.

    The gateway stamps a failed-for-age edit row with
    ``last_error='edit_window_expired'`` (and posted_at, so it is not retried);
    its presence means the base message can no longer be edited.
    """
    hit = session.exec(
        select(WhatsappOutbox)
        .where(WhatsappOutbox.edit_of == checklist_id)
        .where(WhatsappOutbox.last_error == "edit_window_expired")
        .limit(1)
    ).first()
    return hit is not None


def _render(session_key: str):
    """Render the checklist for a session's progress buffer, or None.

    render_checklist lives in chat.bot (which imports discord); imported lazily so
    this module stays importable without the Discord stack at load time.
    """
    from chat.bot import render_checklist

    return render_checklist(_progress_get(session_key))


def _should_edit(
    stages_version: int,
    last_stages_version: int,
    done: bool,
    last_done: bool,
    now: float,
    last_edit_at: float,
) -> bool:
    from chat.bot import _should_edit_checklist

    return _should_edit_checklist(
        stages_version, last_stages_version, done, last_done, now, last_edit_at
    )


def _progress_get(session_key: str):
    from chat import goosecracker_progress as gp

    return gp.get(session_key)


# --- Result delivery --------------------------------------------------------


def enqueue_message_sync(group_jid: str, content: str) -> None:
    """Open a session, enqueue a WhatsApp message, commit.

    The runner's result-delivery half is async, so it hands this sync DB write to
    a worker thread (a sync Session must not run on the event loop). This is the
    WhatsApp counterpart to ``goosecracker.runner._enqueue_sync``.
    """
    with Session(get_engine()) as session:
        enqueue_message(session, group_jid, content=content)
        session.commit()


def household_group_jids() -> list[str]:
    """Return the JIDs of enabled household-tier WhatsApp groups.

    Cross-domain digests (e.g. dr-jobs) deliver by group JID. The JID is PII and
    lives only in the DB (``chat.whatsapp_group``), never in git, so callers read
    it here rather than from a config value. Synchronous; call via
    ``asyncio.to_thread``. Mirrors how the morning digest fans out over groups.
    """
    with Session(get_engine()) as session:
        rows = session.exec(
            select(WhatsappGroup.group_jid).where(
                WhatsappGroup.enabled == True,  # noqa: E712 - SQL boolean, not `is`
                WhatsappGroup.tier == "household",
            )
        ).all()
    return list(rows)


def group_jid_for_session(session_key: str) -> str:
    """Return the raw group JID stored for a WhatsApp session, or "".

    The runner delivers by group JID, not the sanitized session key, so it
    resolves the JID from the row (set at dispatch). Synchronous; call via
    ``asyncio.to_thread``.
    """
    with Session(get_engine()) as session:
        row = session.get(GoosecrackerSession, session_key)
        return row.provider_group_jid if row else ""
