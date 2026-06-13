"""Unit tests for shared.forecast_freshness.top_of_hour."""

from datetime import datetime, timezone

from shared.forecast_freshness import top_of_hour


def test_truncates_mid_hour_to_top_of_hour():
    mid = datetime(2026, 6, 13, 14, 37, 42, 123456, tzinfo=timezone.utc)
    result = top_of_hour(mid)
    assert result == datetime(2026, 6, 13, 14, 0, 0, 0, tzinfo=timezone.utc)
    assert result.minute == 0
    assert result.second == 0
    assert result.microsecond == 0
    assert result.tzinfo == timezone.utc


def test_none_uses_current_utc_time():
    result = top_of_hour(None)
    assert result.minute == 0
    assert result.second == 0
    assert result.microsecond == 0
    assert result.tzinfo == timezone.utc


def test_already_on_the_hour_is_unchanged():
    on_hour = datetime(2026, 6, 13, 9, 0, 0, 0, tzinfo=timezone.utc)
    assert top_of_hour(on_hour) == on_hour


def test_idempotent():
    mid = datetime(2026, 6, 13, 14, 37, 42, 123456, tzinfo=timezone.utc)
    once = top_of_hour(mid)
    assert top_of_hour(once) == once
