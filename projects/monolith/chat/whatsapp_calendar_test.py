"""Tests for chat.whatsapp_calendar credential gating.

The live create-event path talks to Google over httpx and is not exercised here
(it needs a real OAuth credential; that is a live/manual verification). What is
unit-testable is the branch selector the capability layer relies on:
calendar_configured() must be True only when all three OAuth fields are present,
so a partial credential falls back to drafting rather than failing mid-create.
"""

from chat import whatsapp_calendar

_KEYS = (
    "GOOGLE_CALENDAR_CLIENT_ID",
    "GOOGLE_CALENDAR_CLIENT_SECRET",
    "GOOGLE_CALENDAR_REFRESH_TOKEN",
)


def test_configured_requires_all_three(monkeypatch):
    for k in _KEYS:
        monkeypatch.setenv(k, "x")
    assert whatsapp_calendar.calendar_configured() is True


def test_partial_credential_is_unconfigured(monkeypatch):
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_CALENDAR_CLIENT_SECRET", "x")
    monkeypatch.delenv("GOOGLE_CALENDAR_REFRESH_TOKEN", raising=False)
    assert whatsapp_calendar.calendar_configured() is False


def test_unset_is_unconfigured(monkeypatch):
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    assert whatsapp_calendar.calendar_configured() is False


def test_calendar_id_defaults_to_primary(monkeypatch):
    monkeypatch.delenv("GOOGLE_CALENDAR_ID", raising=False)
    assert whatsapp_calendar._calendar_id() == "primary"
    monkeypatch.setenv("GOOGLE_CALENDAR_ID", "joe@example.com")
    assert whatsapp_calendar._calendar_id() == "joe@example.com"
