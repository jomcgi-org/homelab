from datetime import datetime, timedelta, timezone

from core.clock import (
    as_utc,
    aware,
    aware_iso,
    iso,
    iso_or_str,
    iso_z,
    isoformat_or_none,
    utc_now,
)

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


def test_aware_labels_naive_and_keeps_zone():
    naive = datetime(2026, 10, 11, 2, 0)
    assert aware(naive) == naive.replace(tzinfo=timezone.utc)
    stamped = datetime(2026, 10, 11, 3, 0, tzinfo=_BST)
    assert aware(stamped) is stamped


def test_aware_iso_keeps_zone_and_passes_none():
    assert aware_iso(None) is None
    assert aware_iso(datetime(2026, 10, 11, 2, 0, 30)) == "2026-10-11T02:00:30+00:00"
    assert (
        aware_iso(datetime(2026, 10, 11, 3, 0, tzinfo=_BST))
        == "2026-10-11T03:00:00+01:00"
    )


def test_iso_z_uses_a_z_suffix_and_passes_none():
    naive = datetime(2026, 1, 2, 3, 4, 5)
    assert iso_z(naive) == "2026-01-02T03:04:05Z"
    assert (
        iso_z(naive.replace(tzinfo=timezone(timedelta(hours=1))))
        == "2026-01-02T02:04:05Z"
    )
    assert iso_z(None) is None


def test_iso_or_str_stamps_datetimes_and_passes_strings_through():
    assert iso_or_str(None) is None
    assert iso_or_str("2026-01-02") == "2026-01-02"
    assert iso_or_str(7) == "7"
    assert iso_or_str(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05+00:00"


def test_utc_now_is_aware_utc():
    stamp = utc_now()
    assert stamp.tzinfo is timezone.utc
    assert abs((datetime.now(timezone.utc) - stamp).total_seconds()) < 5


def test_isoformat_or_none_keeps_offsets_and_passes_none_through():
    assert isoformat_or_none(None) is None
    assert isoformat_or_none(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05"
    assert (
        isoformat_or_none(datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
        == "2026-01-02T03:04:05+00:00"
    )
