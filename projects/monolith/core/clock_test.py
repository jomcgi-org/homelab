from datetime import datetime, timedelta, timezone

from core.clock import as_utc, iso

_BST = timezone(timedelta(hours=1))


def test_as_utc_none_stays_none():
    assert as_utc(None) is None


def test_as_utc_naive_is_labelled_utc():
    naive = datetime(2026, 10, 11, 2, 0, 0)
    assert as_utc(naive) == naive.replace(tzinfo=timezone.utc)


def test_as_utc_converts_other_zones():
    assert as_utc(datetime(2026, 10, 11, 3, 0, tzinfo=_BST)) == datetime(
        2026, 10, 11, 2, 0, tzinfo=timezone.utc
    )


def test_iso_formats_in_utc():
    assert iso(None) is None
    assert iso(datetime(2026, 10, 11, 2, 0, 30)) == "2026-10-11T02:00:30+00:00"
    assert iso(datetime(2026, 10, 11, 3, 0, tzinfo=_BST)) == "2026-10-11T02:00:00+00:00"
