"""WhatsApp outbox writers (ADR 039, spec section 3).

The monolith enqueues; the single-replica Go gateway drains, sends via
whatsmeow, and stamps posted_at + sent_message_id. Unlike the Discord outbox
there is no Python drain loop here: the gateway is the only sender.

Callers commit the session themselves (matches the rest of the chat write API,
including chat.outbox).
"""

from __future__ import annotations

from sqlmodel import Session

from chat.models import WhatsappOutbox


def enqueue_message(
    session: Session,
    group_jid: str,
    *,
    content: str,
    quoted_message_id: str | None = None,
) -> None:
    """Enqueue a text message to ``group_jid``.

    ``quoted_message_id`` sends it as a reply to that WhatsApp message. Raises
    ValueError on empty content (the DB CHECK also rejects it, but failing here
    gives the caller a clear error instead of an IntegrityError at commit).
    """
    if not content:
        raise ValueError("enqueue_message requires non-empty content")
    session.add(
        WhatsappOutbox(
            group_jid=group_jid,
            kind="message",
            content=content,
            quoted_message_id=quoted_message_id,
        )
    )


def enqueue_message_returning_id(
    session: Session,
    group_jid: str,
    *,
    content: str,
    quoted_message_id: str | None = None,
) -> int:
    """Enqueue a message and return its outbox id (flushes to assign it).

    Used by the WhatsApp checklist path, which must remember the id of the
    checklist message so later ``edit`` rows can reference it (and so a fresh
    repost after the edit window closes can repoint to the new id). Unlike
    ``enqueue_message`` this flushes so ``row.id`` is populated before return;
    the caller still commits.
    """
    if not content:
        raise ValueError("enqueue_message_returning_id requires non-empty content")
    row = WhatsappOutbox(
        group_jid=group_jid,
        kind="message",
        content=content,
        quoted_message_id=quoted_message_id,
    )
    session.add(row)
    session.flush()
    return row.id


def enqueue_edit(
    session: Session,
    group_jid: str,
    edit_of: int,
    content: str,
) -> None:
    """Enqueue an edit of a previous send.

    ``edit_of`` is the outbox id of the original ``message`` row; the gateway
    resolves it to that row's ``sent_message_id`` and edits the live message.
    WhatsApp only allows edits for ~15 minutes: past that window the gateway
    consumes the row with ``last_error='edit_window_expired'`` and the monolith
    reposts a fresh message.
    """
    if not content:
        raise ValueError("enqueue_edit requires non-empty content")
    if edit_of is None:
        raise ValueError("enqueue_edit requires the original send's outbox id")
    session.add(
        WhatsappOutbox(
            group_jid=group_jid,
            kind="edit",
            content=content,
            edit_of=edit_of,
        )
    )


def enqueue_reaction(
    session: Session,
    group_jid: str,
    target_message_id: str,
    target_sender_jid: str,
    reaction: str,
    *,
    remove: bool = False,
) -> None:
    """Enqueue an add/remove of ``reaction`` on ``target_message_id``.

    ``target_sender_jid`` is the JID of the sender of the target message, which
    whatsmeow's reaction build requires (the gateway holds no message history to
    look it up). ``remove=True`` clears the reaction (the gateway sends an empty
    reaction). The ``reaction`` string must still be supplied so the CHECK holds
    and the row records what was cleared.
    """
    if not reaction:
        raise ValueError("enqueue_reaction requires a reaction emoji")
    if not target_message_id or not target_sender_jid:
        raise ValueError(
            "enqueue_reaction requires target_message_id and target_sender_jid"
        )
    session.add(
        WhatsappOutbox(
            group_jid=group_jid,
            kind="reaction",
            target_message_id=target_message_id,
            target_sender_jid=target_sender_jid,
            reaction=reaction,
            reaction_remove=remove,
        )
    )
