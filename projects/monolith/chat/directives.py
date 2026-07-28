"""Living per-channel behavioural directives (ADR 035 phase 5).

Every channel starts from a git-tracked seed (``directive_seed.md``), seeded
lazily on first read. Updates are propose-then-confirm (Task 5.2 owns the
Discord reaction flow); this module owns the storage and the atomic
active-flip. Full history is kept, never mutated in place: every seed,
propose, confirm, and reset inserts a new row.

Per-user style preferences live alongside but are layered on top of the
channel directive at reply time, never merged into it.

All functions are synchronous (open their own session); call via
``asyncio.to_thread`` from the bot's async handlers.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from core.db import get_engine
from chat.models import ChannelDirective, UserStylePref

SEED_PATH = Path(__file__).parent / "directive_seed.md"

# A proposal older than this is expired; apply_proposal refuses to activate
# it. The Task 5.2 reaction handler enforces the same window on its side; this
# is the storage-layer backstop so a stale confirmation can never sneak
# through. By owner decision, anyone with access to the channel can confirm or
# discard a proposal (no per-confirmer authorization check): a directive is
# guarded (tone-only, see guard()) and only rubber-stamps text the agent
# already drafted, so the blast radius is bounded.
PROPOSAL_TTL = timedelta(minutes=10)

# Keyword screen for propose_update: a directive shapes tone/attention/
# interaction style ONLY. It must never be used to grant tools, permissions,
# ACLs, ambient mode, or repo access - those are ADR 029 grants, not
# directives. Keep this list focused: broad enough to catch an attempted
# scope change, narrow enough not to block legitimate tone requests. The
# Task 5.2 update prompt reinforces the same boundary on the LLM side; this
# is the deterministic backstop.
_GUARD_KEYWORDS = (
    "tool",
    "grant",
    "acl",
    "permission",
    "ambient",
    "repo",
    "push to",
    "admin",
)


def _seed_text() -> str:
    return SEED_PATH.read_text().strip()


def _seed_ref() -> str:
    """A short content hash of the seed, stamped onto seeded rows so it's
    obvious which git revision of the seed a channel started from."""
    return hashlib.sha256(_seed_text().encode()).hexdigest()[:16]


def _active_row(session: Session, channel_id: str) -> ChannelDirective | None:
    return session.exec(
        select(ChannelDirective)
        .where(ChannelDirective.channel_id == channel_id)
        .where(ChannelDirective.active == True)  # noqa: E712 - SQL boolean
    ).first()


def get_active(channel_id: str) -> str:
    """The active directive text for a channel, seeding it from git on first
    read. Two concurrent first-reads race to insert; the loser catches the
    partial-unique-index violation, rolls back, and re-reads the winner's
    row (falling back to the seed text itself in the vanishingly unlikely
    case the winner's row isn't visible yet)."""
    with Session(get_engine()) as session:
        row = _active_row(session, channel_id)
        if row is not None:
            return row.directive

    text = _seed_text()
    with Session(get_engine()) as session:
        session.add(
            ChannelDirective(
                channel_id=channel_id,
                directive=text,
                version=1,
                active=True,
                seed_ref=_seed_ref(),
            )
        )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            row = _active_row(session, channel_id)
            return row.directive if row is not None else text
    return text


def get_active_version(channel_id: str) -> int:
    """The active row's version for a channel, 0 if none exists yet."""
    with Session(get_engine()) as session:
        row = _active_row(session, channel_id)
        return row.version if row is not None else 0


def guard(text: str) -> tuple[bool, str]:
    """Reject a proposed directive that tries to change tools/ACLs/
    permissions/ambient/repos. Directives shape tone and interaction style
    only. Returns (False, reason) on reject, (True, "") on ok.

    This is a best-effort keyword screen, NOT a security boundary: a directive
    is only prepended system-prompt text and cannot grant a tool or capability
    regardless of its content (the chat agent's tools are fixed). It guards
    against a directive that reads as if it changes access; the residual
    prompt-injection risk of attacker-influenced system-prompt text is inherent
    and bounded to tone. Matches on whole words so "report"/"repository" do not
    trip the "repo" keyword."""
    lowered = text.lower()
    for keyword in _GUARD_KEYWORDS:
        if re.search(r"\b" + re.escape(keyword) + r"\b", lowered):
            return (
                False,
                f"directives can only shape tone and interaction style, "
                f"not grants or access (blocked on '{keyword}')",
            )
    return True, ""


def propose_update(
    channel_id: str,
    new_text: str,
    user_id: str,
    motivating_message_id: str,
    proposal_message_id: str,
) -> tuple[bool, str]:
    """Guard and stage a proposed directive as an inactive row. Returns
    (False, reason) if the guard rejects it, else (True, "")."""
    ok, reason = guard(new_text)
    if not ok:
        return False, reason
    with Session(get_engine()) as session:
        current = _active_row(session, channel_id)
        session.add(
            ChannelDirective(
                channel_id=channel_id,
                directive=new_text,
                version=(current.version if current is not None else 0) + 1,
                active=False,
                updated_by_user_id=user_id,
                motivating_message_id=motivating_message_id,
                proposal_message_id=proposal_message_id,
                previous_version=current.version if current is not None else 0,
            )
        )
        session.commit()
    return True, ""


def apply_proposal(proposal_message_id: str) -> bool:
    """Flip a fresh (< PROPOSAL_TTL old) inactive proposal to active,
    deactivating the current active row for its channel, in one committed
    transaction. Returns False if there is no matching proposal or it has
    expired; the row is left untouched either way in that case."""
    with Session(get_engine()) as session:
        proposal = session.exec(
            select(ChannelDirective)
            .where(ChannelDirective.proposal_message_id == proposal_message_id)
            .where(ChannelDirective.active == False)  # noqa: E712 - SQL boolean
            .with_for_update()
        ).first()
        if proposal is None:
            return False
        created_at = proposal.created_at
        if created_at.tzinfo is None:
            # SQLite test fixtures store naive datetimes; assume UTC (the
            # only timezone ever written here) so the TTL compare is valid.
            created_at = created_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - created_at > PROPOSAL_TTL:
            return False

        current = session.exec(
            select(ChannelDirective)
            .where(ChannelDirective.channel_id == proposal.channel_id)
            .where(ChannelDirective.active == True)  # noqa: E712 - SQL boolean
            .with_for_update()
        ).first()
        if current is not None:
            current.active = False
        proposal.active = True
        proposal.version = (current.version if current is not None else 0) + 1
        try:
            session.commit()
        except IntegrityError:
            # A concurrent apply/reset activated another row for this channel
            # first; the partial unique index (one active row) rejects this
            # commit. Treat as not-applied rather than raising.
            session.rollback()
            return False
    return True


def reset(channel_id: str, user_id: str, motivating_message_id: str = "") -> None:
    """Create a new active row from the seed, deactivating the current one.
    History is never deleted."""
    with Session(get_engine()) as session:
        current = _active_row(session, channel_id)
        if current is not None:
            current.active = False
        session.add(
            ChannelDirective(
                channel_id=channel_id,
                directive=_seed_text(),
                version=(current.version if current is not None else 0) + 1,
                active=True,
                seed_ref=_seed_ref(),
                updated_by_user_id=user_id,
                motivating_message_id=motivating_message_id,
                previous_version=current.version if current is not None else 0,
            )
        )
        try:
            session.commit()
        except IntegrityError:
            # Lost a race with a concurrent reset/apply for this channel; the
            # other writer's active row stands. No-op rather than raise.
            session.rollback()


def insert_active_directive(
    session: Session,
    channel_id: str,
    text: str,
    *,
    source: str,
    motivating_message_id: str = "",
    updated_by_user_id: str = "",
) -> int:
    """Flip the current active row inactive and add a new active row (``text`` +
    ``source``) WITHIN the caller's ``session``, WITHOUT committing. The caller
    owns the transaction. This is the session-param primitive the autopilot
    shares so its baseline read, directive apply, and log write commit
    atomically in one session (monolith async/session rule: the caller runs it in
    a ``to_thread`` worker with its own session). Returns the new version.
    """
    current = _active_row(session, channel_id)
    new_version = (current.version if current is not None else 0) + 1
    if current is not None:
        current.active = False
        session.flush()  # release the partial-unique active slot before insert
    session.add(
        ChannelDirective(
            channel_id=channel_id,
            directive=text,
            version=new_version,
            active=True,
            source=source,
            updated_by_user_id=updated_by_user_id,
            motivating_message_id=motivating_message_id,
            previous_version=current.version if current is not None else 0,
        )
    )
    return new_version


def set_channel_directive(
    channel_id: str,
    text: str,
    *,
    source: str,
    motivating_message_id: str = "",
    updated_by_user_id: str = "",
) -> int:
    """Own-session wrapper around ``insert_active_directive``: reinstate arbitrary
    ``text`` as the new active row for a channel, deactivating the current one.
    History is never mutated in place. This is the versioned-history primitive
    the autopilot revert path and the MCP manual-set surface share. ``source``
    records provenance for the manual-precedence rule (seed | observer |
    autopilot | manual). Returns the new active version, or the current version
    unchanged if a concurrent writer won the active-row race.

    Unlike ``reset``/``propose_update`` this reinstates arbitrary text (a revert
    passes the prior row's stored text), so it never re-derives from the seed.
    """
    with Session(get_engine()) as session:
        new_version = insert_active_directive(
            session,
            channel_id,
            text,
            source=source,
            motivating_message_id=motivating_message_id,
            updated_by_user_id=updated_by_user_id,
        )
        try:
            session.commit()
        except IntegrityError:
            # Lost a race with a concurrent apply/reset for this channel; the
            # other writer's active row stands. Report the current version.
            session.rollback()
            row = _active_row(session, channel_id)
            return row.version if row is not None else 0
    return new_version


def pin_channel_directive(channel_id: str) -> bool:
    """Mark a channel's active directive row ``source='manual'`` in place so the
    autopilot leaves it alone. This stamps provenance, not directive text, so it
    is not a versioned change (no new row). Returns False if the channel has no
    active row yet."""
    with Session(get_engine()) as session:
        row = _active_row(session, channel_id)
        if row is None:
            return False
        row.source = "manual"
        session.add(row)
        session.commit()
    return True


def pin_style_pref(user_id: str) -> bool:
    """Mark a user's active style pref ``source='manual'`` in place so the
    autopilot leaves it alone. Returns False if the user has no active pref."""
    with Session(get_engine()) as session:
        row = session.exec(
            select(UserStylePref)
            .where(UserStylePref.user_id == user_id)
            .where(UserStylePref.active == True)  # noqa: E712 - SQL boolean
        ).first()
        if row is None:
            return False
        row.source = "manual"
        session.add(row)
        session.commit()
    return True


def is_proposal(proposal_message_id: str) -> bool:
    """True if a channel_directive row was staged with this proposal message
    id (active or not -- an already-applied or discarded proposal is still a
    proposal). Lets the reaction handler ignore 👍/👎 on unrelated messages."""
    with Session(get_engine()) as session:
        row = session.exec(
            select(ChannelDirective).where(
                ChannelDirective.proposal_message_id == proposal_message_id
            )
        ).first()
        return row is not None


def get_style_pref(user_id: str) -> str:
    """The user's active style preference text, "" if none is set."""
    with Session(get_engine()) as session:
        row = session.exec(
            select(UserStylePref)
            .where(UserStylePref.user_id == user_id)
            .where(UserStylePref.active == True)  # noqa: E712 - SQL boolean
        ).first()
        return row.pref if row is not None else ""


def insert_active_pref(
    session: Session,
    user_id: str,
    pref: str,
    *,
    source: str,
    motivating_message_id: str = "",
) -> None:
    """Flip the user's current active pref inactive and add a new active pref
    (``pref`` + ``source``) WITHIN the caller's ``session``, WITHOUT committing.
    The caller owns the transaction, so the autopilot can commit its pref apply
    and its log row atomically. Companion of ``insert_active_directive``."""
    current = session.exec(
        select(UserStylePref)
        .where(UserStylePref.user_id == user_id)
        .where(UserStylePref.active == True)  # noqa: E712 - SQL boolean
        .with_for_update()
    ).first()
    if current is not None:
        current.active = False
        session.flush()  # release the partial-unique active slot before insert
    session.add(
        UserStylePref(
            user_id=user_id,
            pref=pref,
            active=True,
            updated_by_user_id=user_id,
            motivating_message_id=motivating_message_id,
            source=source,
        )
    )


def set_style_pref(
    user_id: str,
    pref: str,
    motivating_message_id: str = "",
    *,
    source: str = "manual",
) -> None:
    """Deactivate the user's current active preference (if any) and insert
    the new one as active. History is never deleted. ``source`` defaults to
    'manual' because a user setting their own style is a human action that must
    win over the autopilot; the autopilot passes source='autopilot' (both for an
    apply and for a revert, which reinstates the prior pref text)."""
    with Session(get_engine()) as session:
        insert_active_pref(
            session,
            user_id,
            pref,
            source=source,
            motivating_message_id=motivating_message_id,
        )
        try:
            session.commit()
        except IntegrityError:
            # Lost a race with a concurrent style-pref write for this user; the
            # other writer's active pref stands. No-op rather than raise.
            session.rollback()
