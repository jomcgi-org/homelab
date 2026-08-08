"""WhatsApp household group lookup and delivery helpers."""

from __future__ import annotations

from sqlmodel import Session, select

from chat.models import WhatsappGroup
from chat.whatsapp_outbox import enqueue_message
from core.db import get_engine


def enqueue_message_sync(group_jid: str, content: str) -> None:
    """Open a session, enqueue a WhatsApp message, commit.

    Callers on the event loop (the scheduled cross-domain digests) hand this to
    a worker thread: a sync Session must not run on the loop.
    """
    with Session(get_engine()) as session:
        enqueue_message(session, group_jid, content=content)
        session.commit()


def household_group_jids() -> list[str]:
    """Return the JIDs of enabled household-tier WhatsApp groups.

    The JID is PII and lives only in the database, so scheduled cross-domain
    digests resolve their recipients here instead of from configuration.
    """
    with Session(get_engine()) as session:
        rows = session.exec(
            select(WhatsappGroup.group_jid).where(
                WhatsappGroup.enabled == True,  # noqa: E712 - SQL boolean
                WhatsappGroup.tier == "household",
            )
        ).all()
    return list(rows)
