import asyncio
import json
import logging
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
from icalendar import Calendar
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlmodel import Session, text

logger = logging.getLogger(__name__)
TZ = ZoneInfo("America/Vancouver")

ICAL_FEED_URL = os.environ.get("ICAL_FEED_URL", "")


def parse_events_for_date(ics_text: str, target_date: date, tz: ZoneInfo) -> list[dict]:
    cal = Calendar.from_ical(ics_text)
    all_day = []
    timed = []
    seen: set[tuple[str | None, str]] = set()

    for component in cal.walk("VEVENT"):
        dtstart = component.get("DTSTART")
        if dtstart is None:
            continue
        dt = dtstart.dt
        summary = str(component.get("SUMMARY", ""))

        # All-day event: dtstart is a date, not datetime
        if isinstance(dt, date) and not isinstance(dt, datetime):
            if dt == target_date:
                key = (None, summary)
                if key not in seen:
                    seen.add(key)
                    all_day.append({"time": None, "title": summary, "allDay": True})
            continue

        # Timed event: convert to target timezone
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        else:
            dt = dt.astimezone(tz)

        if dt.date() == target_date:
            time_str = dt.strftime("%H:%M")
            key = (time_str, summary)
            if key not in seen:
                seen.add(key)
                # Parse end time
                dtend = component.get("DTEND")
                end_str = None
                if dtend is not None:
                    dte = dtend.dt
                    if isinstance(dte, datetime):
                        if dte.tzinfo is None:
                            dte = dte.replace(tzinfo=tz)
                        else:
                            dte = dte.astimezone(tz)
                        end_str = dte.strftime("%H:%M")
                timed.append(
                    {
                        "time": time_str,
                        "endTime": end_str,
                        "title": summary,
                        "allDay": False,
                    }
                )

    timed.sort(key=lambda e: e["time"])
    return all_day + timed


def get_today_events(session: Session) -> list[dict]:
    """Return the snapshotted events for today.

    Reads the single home.calendar_snapshot row written by poll_calendar. If the
    snapshot is for an earlier day (the poll has not run yet today) or is absent,
    returns an empty list rather than stale events. A missing table (the SQLite
    test fixtures build tables from SQLModel metadata, not migrations, and this
    snapshot is raw-SQL; likewise a not-yet-migrated environment) degrades to an
    empty list so the home page renders rather than 500ing.
    """
    try:
        row = session.execute(
            text("SELECT event_date, events FROM home.calendar_snapshot WHERE id = 1")
        ).first()
    except (OperationalError, ProgrammingError):
        session.rollback()
        return []
    if row is None:
        return []
    event_date, events = row
    today = datetime.now(TZ).date()
    # SQLite test fixtures hand back event_date as an ISO string; Postgres as a
    # date. Normalise before comparing.
    if isinstance(event_date, str):
        event_date = date.fromisoformat(event_date)
    if event_date != today:
        return []
    return list(events)


def _write_calendar_snapshot(event_date: date, events: list[dict]) -> None:
    """Upsert today's parsed events into the single snapshot row. Opens its own
    session so it can run in a worker thread off the event loop."""
    from core.db import get_engine

    with Session(get_engine()) as session:
        session.execute(
            text(
                """
                INSERT INTO home.calendar_snapshot (id, event_date, events, snapshot_at)
                VALUES (1, :event_date, :events, now())
                ON CONFLICT (id) DO UPDATE
                    SET event_date = EXCLUDED.event_date,
                        events = EXCLUDED.events,
                        snapshot_at = EXCLUDED.snapshot_at
                """
            ),
            {"event_date": event_date, "events": json.dumps(events)},
        )
        session.commit()


async def poll_calendar() -> None:
    if not ICAL_FEED_URL:
        logger.warning("ICAL_FEED_URL not set, skipping calendar poll")
        return
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.get(ICAL_FEED_URL, timeout=30)
            resp.raise_for_status()
        today = datetime.now(TZ).date()
        events = parse_events_for_date(resp.text, today, TZ)
        await asyncio.to_thread(_write_calendar_snapshot, today, events)
        logger.info("Calendar refreshed: %d events for %s", len(events), today)
    except Exception:
        logger.exception("Failed to fetch calendar feed")


async def calendar_poll_handler() -> None:
    """Scheduler handler for calendar polling (stateless HTTP fetch)."""
    await poll_calendar()
    return None
