"""Best-effort natural-language datetime parsing for the WhatsApp household
capabilities (ADR 039 spec section 5). Stdlib only, deliberately: it handles the
phrasings a household actually uses (a weekday, today/tonight/tomorrow, and a
clock time) and returns ``None`` when it cannot find a usable time, so the caller
asks one clarifying question (clarify-once) rather than guess a wrong slot.

``python-dateutil`` is not in the backend's Bazel dependency closure, so pulling
it in for this would be an undeclared-dependency risk in CI; a small hand-rolled
parser over ``datetime``/``re``/``zoneinfo`` is enough for the intended phrasings
and keeps this module self-contained and cheaply testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Weekday name (full or three-letter) -> Python weekday index (Mon=0).
_WEEKDAYS = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}

# Named times of day -> (hour, minute). Presence of one of these counts as an
# explicit time, so an event with "tomorrow evening" is not treated as ambiguous.
_NAMED_TIMES = {
    "midnight": (0, 0),
    "noon": (12, 0),
    "midday": (12, 0),
    "morning": (9, 0),
    "afternoon": (14, 0),
    "evening": (19, 0),
    "tonight": (19, 0),
}

# Default hour used when a date is given with no time at all (e.g. "on Friday").
_DEFAULT_HOUR = 9

_TIME_HHMM = re.compile(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?\b", re.IGNORECASE)
_TIME_HAM = re.compile(r"\b(\d{1,2})\s*(am|pm)\b", re.IGNORECASE)
_TIME_AT = re.compile(r"\bat\s+(\d{1,2})\b", re.IGNORECASE)


@dataclass
class ParsedWhen:
    """A parsed datetime and whether the source text carried an explicit time.

    ``when`` is timezone-aware in the caller's tz. ``had_time`` is True when the
    text named a clock time (``7pm``, ``at 19``, ``noon``, ``tonight``); False
    when only a date was found and the default hour was applied. Scheduling
    requires ``had_time`` (else it clarifies); reminders accept either.
    """

    when: datetime
    had_time: bool


def _match_weekday(text: str) -> int | None:
    for name, idx in _WEEKDAYS.items():
        if re.search(rf"\b{name}\b", text):
            return idx
    return None


def _extract_time(text: str) -> tuple[int, int] | None:
    """Return (hour, minute) for the first clock time in ``text``, or None."""
    for name, hm in _NAMED_TIMES.items():
        if re.search(rf"\b{name}\b", text):
            return hm

    m = _TIME_HHMM.search(text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2))
        ampm = (m.group(3) or "").lower()
        hour = _apply_ampm(hour, ampm)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute

    m = _TIME_HAM.search(text)
    if m:
        hour = _apply_ampm(int(m.group(1)), m.group(2).lower())
        if 0 <= hour <= 23:
            return hour, 0

    m = _TIME_AT.search(text)
    if m:
        hour = int(m.group(1))
        if 0 <= hour <= 23:
            return hour, 0

    return None


def _apply_ampm(hour: int, ampm: str) -> int:
    if ampm == "pm" and hour < 12:
        return hour + 12
    if ampm == "am" and hour == 12:
        return 0
    return hour


def parse_datetime(text: str, *, now: datetime, tz: ZoneInfo) -> ParsedWhen | None:
    """Parse ``text`` into a future-leaning datetime, or None if no time is found.

    ``now`` must be timezone-aware; it is converted into ``tz`` to anchor relative
    words. Recognises today/tonight/tomorrow and weekday names for the date, and
    HH:MM, ``7pm``, ``at 19``, and named times of day for the clock time. When a
    time is given without a date, it lands today, rolling to tomorrow if that
    instant has already passed. Returns None only when neither a date nor a time
    is present, which is the signal for the caller to ask one clarifying question.
    """
    lowered = text.lower()
    now_local = now.astimezone(tz)
    today = now_local.date()

    date_found = False
    target_date = today
    # True when the date came from a weekday name that resolves to today (e.g.
    # "Saturday" said on a Saturday). Such a match with a past time-of-day means
    # next week, not today in the past; see the roll-forward below.
    weekday_today = False

    if re.search(r"\btomorrow\b", lowered):
        target_date = today + timedelta(days=1)
        date_found = True
    elif re.search(r"\btoday\b", lowered) or re.search(r"\btonight\b", lowered):
        target_date = today
        date_found = True
    else:
        wd = _match_weekday(lowered)
        if wd is not None:
            ahead = (wd - today.weekday()) % 7
            if re.search(r"\bnext\b", lowered) and ahead == 0:
                ahead = 7
            elif ahead == 0:
                weekday_today = True
            target_date = today + timedelta(days=ahead)
            date_found = True

    time_hm = _extract_time(lowered)

    if not date_found and time_hm is None:
        return None

    if time_hm is not None:
        hour, minute = time_hm
        had_time = True
    else:
        hour, minute = _DEFAULT_HOUR, 0
        had_time = False

    when = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        hour,
        minute,
        tzinfo=tz,
    )

    # A bare time ("7pm") with no date lands today; if already past, roll forward
    # a day so "remind me at 7pm" said at 8pm means tomorrow, not the past.
    if not date_found and when <= now_local:
        when = when + timedelta(days=1)
    # A weekday name resolving to today with a past time means next week ("dinner
    # Sat 6pm" said Saturday evening is next Saturday), not today in the past.
    elif weekday_today and when <= now_local:
        when = when + timedelta(days=7)

    return ParsedWhen(when=when, had_time=had_time)
