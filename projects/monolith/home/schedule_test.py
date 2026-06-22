from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import httpx
import pytest

import home.schedule as svc
from home.schedule import get_today_events, parse_events_for_date, poll_calendar


class _RowSession:
    """Fake session whose execute().first() returns a fixed (event_date, events)
    tuple, or None for the no-snapshot case."""

    def __init__(self, row):
        self._row = row

    def execute(self, *_a, **_k):
        result = MagicMock()
        result.first = MagicMock(return_value=self._row)
        return result


def _today():
    return datetime.now(svc.TZ).date()


def test_get_today_events_returns_events_for_today():
    events = [{"title": "Standup", "time": "09:00", "allDay": False}]
    assert get_today_events(_RowSession((_today(), events))) == events


def test_get_today_events_empty_when_no_snapshot():
    assert get_today_events(_RowSession(None)) == []


def test_get_today_events_empty_when_snapshot_is_stale():
    events = [{"title": "Yesterday", "time": None, "allDay": True}]
    assert get_today_events(_RowSession((date(2000, 1, 1), events))) == []


TZ = ZoneInfo("America/Vancouver")

# Minimal ICS with one timed event and one all-day event
SAMPLE_ICS = """\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
DTSTART:20260330T090000
DTEND:20260330T093000
SUMMARY:Standup
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20260330
DTEND;VALUE=DATE:20260331
SUMMARY:Company Holiday
END:VEVENT
BEGIN:VEVENT
DTSTART:20260331T140000
DTEND:20260331T150000
SUMMARY:Tomorrow Event
END:VEVENT
END:VCALENDAR
"""


def test_parse_timed_event():
    events = parse_events_for_date(SAMPLE_ICS, date(2026, 3, 30), TZ)
    timed = [e for e in events if not e["allDay"]]
    assert len(timed) == 1
    assert timed[0]["time"] == "09:00"
    assert timed[0]["endTime"] == "09:30"
    assert timed[0]["title"] == "Standup"


def test_parse_all_day_event():
    events = parse_events_for_date(SAMPLE_ICS, date(2026, 3, 30), TZ)
    all_day = [e for e in events if e["allDay"]]
    assert len(all_day) == 1
    assert all_day[0]["title"] == "Company Holiday"
    assert all_day[0]["time"] is None


def test_all_day_events_come_first():
    events = parse_events_for_date(SAMPLE_ICS, date(2026, 3, 30), TZ)
    assert events[0]["allDay"] is True
    assert events[1]["allDay"] is False


def test_excludes_other_dates():
    events = parse_events_for_date(SAMPLE_ICS, date(2026, 3, 30), TZ)
    titles = [e["title"] for e in events]
    assert "Tomorrow Event" not in titles


def test_empty_calendar():
    ics = "BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n"
    events = parse_events_for_date(ics, date(2026, 3, 30), TZ)
    assert events == []


DUPLICATE_ICS = """\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
DTSTART:20260330T094500
DTEND:20260330T103000
SUMMARY:Infra Ops Review
END:VEVENT
BEGIN:VEVENT
DTSTART:20260330T094500
DTEND:20260330T103000
SUMMARY:Infra Ops Review
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20260330
DTEND;VALUE=DATE:20260331
SUMMARY:Holiday
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20260330
DTEND;VALUE=DATE:20260331
SUMMARY:Holiday
END:VEVENT
END:VCALENDAR
"""


def test_deduplicates_events():
    events = parse_events_for_date(DUPLICATE_ICS, date(2026, 3, 30), TZ)
    assert len(events) == 2
    assert events[0]["title"] == "Holiday"
    assert events[1]["title"] == "Infra Ops Review"
    assert events[1]["endTime"] == "10:30"


NO_DTEND_ICS = """\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
DTSTART:20260330T120000
SUMMARY:Lunch
END:VEVENT
END:VCALENDAR
"""


def test_missing_dtend_returns_none():
    events = parse_events_for_date(NO_DTEND_ICS, date(2026, 3, 30), TZ)
    assert len(events) == 1
    assert events[0]["endTime"] is None


# ---------------------------------------------------------------------------
# poll_calendar() — async function tests
# ---------------------------------------------------------------------------


def _mock_client(*, get=None, response=None):
    """Build an httpx.AsyncClient-shaped async-context mock."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=get) if get else AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
async def test_poll_calendar_skips_when_url_not_set():
    """poll_calendar returns without writing a snapshot when ICAL_FEED_URL is empty."""
    with (
        patch.object(svc, "ICAL_FEED_URL", ""),
        patch.object(svc, "_write_calendar_snapshot") as write,
    ):
        await poll_calendar()
    write.assert_not_called()


@pytest.mark.asyncio
async def test_poll_calendar_handles_network_failure():
    """Network error during fetch is caught; no snapshot is written."""
    client = _mock_client(get=httpx.ConnectError("connection refused"))
    with (
        patch.object(svc, "ICAL_FEED_URL", "http://example.com/calendar.ics"),
        patch("home.schedule.httpx.AsyncClient", return_value=client),
        patch.object(svc, "_write_calendar_snapshot") as write,
    ):
        await poll_calendar()
    write.assert_not_called()


@pytest.mark.asyncio
async def test_poll_calendar_handles_http_error_response():
    """HTTP error status (e.g. 500) is caught via raise_for_status; no write."""
    response = MagicMock()
    response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "500 Internal Server Error", request=MagicMock(), response=MagicMock()
        )
    )
    client = _mock_client(response=response)
    with (
        patch.object(svc, "ICAL_FEED_URL", "http://example.com/calendar.ics"),
        patch("home.schedule.httpx.AsyncClient", return_value=client),
        patch.object(svc, "_write_calendar_snapshot") as write,
    ):
        await poll_calendar()
    write.assert_not_called()


@pytest.mark.asyncio
async def test_poll_calendar_handles_malformed_ical():
    """A parse error from malformed iCal bytes is caught; no snapshot is written.

    Exercises the real parse path: Calendar.from_ical() raises on garbage data,
    and poll_calendar()'s broad except clause must absorb it without writing.
    """
    response = MagicMock()
    response.raise_for_status = MagicMock(return_value=None)
    response.text = "NOT-ICAL\x00\xff garbage data that no parser can handle"
    client = _mock_client(response=response)
    with (
        patch.object(svc, "ICAL_FEED_URL", "http://example.com/calendar.ics"),
        patch("home.schedule.httpx.AsyncClient", return_value=client),
        patch.object(svc, "_write_calendar_snapshot") as write,
    ):
        await poll_calendar()
    write.assert_not_called()


@pytest.mark.asyncio
async def test_poll_calendar_writes_snapshot_on_success():
    """On a successful fetch, the parsed events are written to the snapshot."""
    valid_events = [{"title": "Standup", "time": "09:00", "allDay": False}]
    response = MagicMock()
    response.raise_for_status = MagicMock(return_value=None)
    response.text = "BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n"
    client = _mock_client(response=response)
    with (
        patch.object(svc, "ICAL_FEED_URL", "http://example.com/calendar.ics"),
        patch("home.schedule.httpx.AsyncClient", return_value=client),
        patch.object(svc, "parse_events_for_date", return_value=valid_events),
        patch.object(svc, "_write_calendar_snapshot") as write,
    ):
        await poll_calendar()
    write.assert_called_once()
    # Second positional arg is the parsed events list.
    assert write.call_args.args[1] == valid_events
