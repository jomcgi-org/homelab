"""WhatsApp household group lookup helpers."""

from __future__ import annotations

from sqlmodel import Session, select

from chat.models import WhatsappGroup
from core.db import get_engine


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
