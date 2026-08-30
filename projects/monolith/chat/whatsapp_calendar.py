"""Google Calendar create-event for the household scheduling capability (ADR 039
spec section 5b, open question 1).

There is no cluster-side Google Calendar integration to reuse: the existing
``home.schedule`` calendar is a read-only iCalendar feed poll, not a write path.
So this resolves ADR 039 OQ1 by provisioning a dedicated cluster-side OAuth user
credential (client id + secret + a long-lived refresh token), all 1Password-
managed and injected only when the WhatsApp gateway is enabled. It talks to the
Calendar REST API over ``httpx`` (already a dependency, so no new Python package):
exchange the refresh token for a short-lived access token, then insert the event.

When the credential is absent, ``calendar_configured()`` is False and the caller
falls back to drafting the event into the morning digest for manual confirmation
(spec 5b fallback). The two branches are selected at runtime on credential
presence, so a deploy without the credential still works, just via drafts.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

import httpx

from shared import google_calendar

logger = logging.getLogger(__name__)

# Default event length when the request names only a start time.
_DEFAULT_DURATION = timedelta(hours=1)


def calendar_configured() -> bool:
    """Whether the OAuth credential for live calendar writes is present.

    All three of client id, client secret, and refresh token must be set; a
    partial credential is treated as unconfigured so the caller takes the safe
    draft fallback rather than failing mid-create.
    """
    return google_calendar.configured()


def _calendar_id() -> str:
    return os.environ.get("GOOGLE_CALENDAR_ID", "primary")


async def create_event(
    *,
    title: str,
    start_at: datetime,
    end_at: datetime | None = None,
    attendees: str | None = None,
) -> dict:
    """Create a calendar event and return the created-event JSON.

    ``start_at`` must be timezone-aware; ``end_at`` defaults to one hour later.
    ``attendees`` is a human-readable string recorded in the description (v1 does
    not resolve WhatsApp contacts to calendar invitees). Raises on any transport
    or API error so the caller can fall back to a draft.
    """
    end = end_at or (start_at + _DEFAULT_DURATION)
    body: dict = {
        "summary": title,
        "start": {"dateTime": start_at.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }
    if attendees:
        body["description"] = f"With: {attendees}"

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        created = await google_calendar._insert_event_json(
            client,
            _calendar_id(),
            body,
        )
    logger.info("whatsapp calendar: created event %s", created.get("id", "?"))
    return created
