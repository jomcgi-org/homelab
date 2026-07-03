"""Tests for chat.whatsapp_timeparse: the stdlib natural-language time parser.

Anchored on a fixed ``now`` (a Wednesday) so relative words resolve
deterministically. Covers weekday+time, tomorrow, bare-time roll-forward, the
date-without-time (had_time False) case, and the unparseable -> None signal the
capability layer treats as "ask one clarifying question".
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from chat.whatsapp_timeparse import parse_datetime

_TZ = ZoneInfo("America/Vancouver")
# 2026-07-01 is a Wednesday. 18:00 UTC = 11:00 local (PDT, UTC-7).
_NOW = datetime(2026, 7, 1, 18, 0, tzinfo=timezone.utc)


def _parse(text):
    return parse_datetime(text, now=_NOW, tz=_TZ)


def test_weekday_and_time():
    p = _parse("dinner with Sam Friday 7pm")
    assert p is not None
    assert p.had_time is True
    assert p.when.weekday() == 4  # Friday
    assert (p.when.hour, p.when.minute) == (19, 0)


def test_tomorrow_at_time():
    p = _parse("remind us tomorrow at 9am")
    assert p is not None
    assert p.had_time is True
    assert p.when.date() == datetime(2026, 7, 2).date()
    assert p.when.hour == 9


def test_hhmm_with_pm():
    p = _parse("call at 7:30pm on Friday")
    assert p is not None
    assert (p.when.hour, p.when.minute) == (19, 30)


def test_date_without_time_has_had_time_false():
    p = _parse("on Friday")
    assert p is not None
    assert p.had_time is False
    assert p.when.hour == 9  # default hour


def test_bare_time_rolls_forward_when_past():
    # 08:00 local is before 11:00 (now), so a bare "8am" means tomorrow.
    p = _parse("at 8am")
    assert p is not None
    assert p.when.date() == datetime(2026, 7, 2).date()
    assert p.when.hour == 8


def test_named_time_counts_as_time():
    p = _parse("dinner tomorrow evening")
    assert p is not None
    assert p.had_time is True
    assert p.when.hour == 19


def test_unparseable_returns_none():
    assert _parse("booked the cabin for the summer") is None
    assert _parse("hello there") is None
