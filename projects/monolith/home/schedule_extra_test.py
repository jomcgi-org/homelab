"""Extra coverage tests for home/schedule.py — timezone-aware events,
date-type DTEND, get_today_events(), and DTSTART with no value."""

from datetime import date, datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from sqlalchemy.exc import OperationalError

import home.schedule as svc
from home.schedule import get_today_events, parse_events_for_date

TZ = ZoneInfo("America/Vancouver")


# ---------------------------------------------------------------------------
# get_today_events — reads the home.calendar_snapshot row
# ---------------------------------------------------------------------------


def _session(row=None, *, raises=None):
    """Fake session whose execute().first() returns `row`, or whose execute()
    raises `raises` (to exercise the missing-table degradation)."""
    session = MagicMock()
    if raises is not None:
        session.execute = MagicMock(side_effect=raises)
    else:
        result = MagicMock()
        result.first = MagicMock(return_value=row)
        session.execute = MagicMock(return_value=result)
    return session


def _today():
    return datetime.now(svc.TZ).date()


class TestGetTodayEvents:
    def test_returns_empty_when_no_snapshot(self):
        """No snapshot row -> []."""
        assert get_today_events(_session(None)) == []

    def test_returns_events_for_today(self):
        """A snapshot dated today returns its events."""
        events = [{"time": "09:00", "title": "Standup", "allDay": False}]
        assert get_today_events(_session((_today(), events))) == events

    def test_returns_empty_when_stale(self):
        """A snapshot from an earlier day returns [] (not stale events)."""
        events = [{"time": None, "title": "Yesterday", "allDay": True}]
        assert get_today_events(_session((date(2000, 1, 1), events))) == []

    def test_returns_empty_when_table_missing(self):
        """A missing table degrades to [] rather than raising (SQLite fixtures /
        not-yet-migrated environments)."""
        err = OperationalError("no such table", None, Exception())
        assert get_today_events(_session(raises=err)) == []

    def test_returns_all_events(self):
        """Every event in the snapshot is returned."""
        events = [
            {"time": None, "title": "Holiday", "allDay": True},
            {"time": "10:00", "title": "Sync", "allDay": False},
            {"time": "14:00", "title": "Demo", "allDay": False},
        ]
        assert len(get_today_events(_session((_today(), events)))) == 3


# ---------------------------------------------------------------------------
# parse_events_for_date — timezone-aware DTSTART
# ---------------------------------------------------------------------------

# ICS with a UTC-stamped timed event on 2026-03-30
TIMEZONE_AWARE_ICS = """\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
DTSTART:20260330T170000Z
DTEND:20260330T183000Z
SUMMARY:UTC Meeting
END:VEVENT
END:VCALENDAR
"""

# ICS where DTEND is a date value (not a datetime)
DATE_DTEND_ICS = """\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
DTSTART:20260330T090000
DTEND;VALUE=DATE:20260331
SUMMARY:Standup with date DTEND
END:VEVENT
END:VCALENDAR
"""

# ICS where DTSTART element is absent (malformed, should be skipped)
MISSING_DTSTART_ICS = """\
BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:No Start Time
DTEND:20260330T100000
END:VEVENT
BEGIN:VEVENT
DTSTART:20260330T110000
SUMMARY:Valid Event
END:VEVENT
END:VCALENDAR
"""


class TestParseEventsTimezoneAware:
    def test_utc_event_converted_to_target_tz(self):
        """A UTC DTSTART is converted to the target timezone for date matching."""
        # 2026-03-30T17:00:00Z  ==  2026-03-30T10:00:00 America/Vancouver
        events = parse_events_for_date(TIMEZONE_AWARE_ICS, date(2026, 3, 30), TZ)
        assert len(events) == 1
        assert events[0]["title"] == "UTC Meeting"
        assert events[0]["allDay"] is False

    def test_utc_event_time_converted_correctly(self):
        """Time field reflects the target-timezone local time, not UTC."""
        events = parse_events_for_date(TIMEZONE_AWARE_ICS, date(2026, 3, 30), TZ)
        assert events[0]["time"] == "10:00"  # UTC 17:00 → Vancouver 10:00

    def test_utc_event_end_time_converted(self):
        """endTime field is also converted from UTC to the target timezone."""
        events = parse_events_for_date(TIMEZONE_AWARE_ICS, date(2026, 3, 30), TZ)
        assert events[0]["endTime"] == "11:30"  # UTC 18:30 → Vancouver 11:30

    def test_utc_event_excluded_for_wrong_date(self):
        """UTC event is excluded when asking for a different local date."""
        events = parse_events_for_date(TIMEZONE_AWARE_ICS, date(2026, 3, 29), TZ)
        assert events == []


class TestParseEventsDateTypeDtend:
    def test_date_dtend_yields_none_end_time(self):
        """When DTEND is a date value (not datetime), endTime is None."""
        events = parse_events_for_date(DATE_DTEND_ICS, date(2026, 3, 30), TZ)
        assert len(events) == 1
        assert events[0]["endTime"] is None

    def test_date_dtend_event_still_included(self):
        """An event with a date-type DTEND is still returned."""
        events = parse_events_for_date(DATE_DTEND_ICS, date(2026, 3, 30), TZ)
        assert events[0]["title"] == "Standup with date DTEND"

    def test_date_dtend_allday_false(self):
        """A timed event is still allDay=False even with a date-type DTEND."""
        events = parse_events_for_date(DATE_DTEND_ICS, date(2026, 3, 30), TZ)
        assert events[0]["allDay"] is False


class TestParseEventsMissingDtstart:
    def test_event_without_dtstart_is_skipped(self):
        """A VEVENT component without DTSTART is silently skipped."""
        events = parse_events_for_date(MISSING_DTSTART_ICS, date(2026, 3, 30), TZ)
        # Only the valid event (11:00) should be returned
        assert len(events) == 1
        assert events[0]["title"] == "Valid Event"

    def test_valid_event_after_missing_dtstart_is_included(self):
        """Events following a malformed VEVENT are still processed."""
        events = parse_events_for_date(MISSING_DTSTART_ICS, date(2026, 3, 30), TZ)
        assert events[0]["time"] == "11:00"
