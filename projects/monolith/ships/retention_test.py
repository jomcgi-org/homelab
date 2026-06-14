"""Unit tests for the pure ships retention helpers.

Only the platform-independent helpers and DDL builders are tested here. The
live partition DDL is Postgres-only and is exercised in CI-against-Postgres /
prod, not in these SQLite-friendly unit tests.
"""

from datetime import date

from ships.heat import bank_day_sql
from ships.retention import (
    create_partition_sql,
    drop_partition_sql,
    partition_bounds,
    partition_name,
    partitions_to_create,
    partitions_to_drop,
)


def test_partition_name():
    assert partition_name(date(2026, 6, 11)) == "positions_20260611"


def test_partition_name_zero_pads():
    assert partition_name(date(2026, 1, 5)) == "positions_20260105"


def test_partition_bounds():
    lo, hi = partition_bounds(date(2026, 6, 11))
    assert lo == "2026-06-11"
    assert hi == "2026-06-12"


def test_partition_bounds_crosses_month():
    lo, hi = partition_bounds(date(2026, 6, 30))
    assert lo == "2026-06-30"
    assert hi == "2026-07-01"


def test_partitions_to_create():
    today = date(2026, 6, 11)
    assert partitions_to_create(today, 2) == [
        date(2026, 6, 11),
        date(2026, 6, 12),
        date(2026, 6, 13),
    ]


def test_partitions_to_create_zero_ahead():
    today = date(2026, 6, 11)
    assert partitions_to_create(today, 0) == [date(2026, 6, 11)]


def test_partitions_to_drop_never_includes_in_retention_days():
    """Critical safety property: a partition holding data within the retention
    window is NEVER returned for dropping."""
    today = date(2026, 6, 11)
    retention_days = 7
    droppable = partitions_to_drop(today, retention_days, scan_days=30)

    # No day within the last `retention_days` days (the retention window) may be
    # dropped. The boundary day today - retention_days still holds data with
    # recorded_at == today - retention_days, which is within retention.
    for offset in range(retention_days + 1):  # 0..7 inclusive
        in_window = date(2026, 6, 11) - _days(offset)
        assert in_window not in droppable, f"{in_window} is within retention"


def test_partitions_to_drop_includes_old_day():
    today = date(2026, 6, 11)
    ten_days_old = today - _days(10)
    assert ten_days_old in partitions_to_drop(today, retention_days=7, scan_days=30)


def test_partitions_to_drop_boundary_day_excluded_neighbor_included():
    today = date(2026, 6, 11)
    droppable = partitions_to_drop(today, retention_days=7, scan_days=30)
    # today - 7 is the boundary partition (still in retention) -> excluded.
    assert (today - _days(7)) not in droppable
    # today - 8 is the newest fully-expired partition -> included.
    assert (today - _days(8)) in droppable


def test_partitions_to_drop_scan_window_bounds():
    today = date(2026, 6, 11)
    droppable = partitions_to_drop(today, retention_days=7, scan_days=30)
    # oldest = today - (7 + 30) = today - 37, newest = today - 8.
    assert min(droppable) == today - _days(37)
    assert max(droppable) == today - _days(8)
    # 37 - 8 + 1 = 30 partitions in the scan window.
    assert len(droppable) == 30


def test_create_partition_sql():
    assert create_partition_sql(date(2026, 6, 11)) == (
        "CREATE TABLE IF NOT EXISTS ships.positions_20260611 "
        "PARTITION OF ships.positions FOR VALUES FROM ('2026-06-11') TO ('2026-06-12')"
    )


def test_drop_partition_sql():
    assert (
        drop_partition_sql(date(2026, 6, 11))
        == "DROP TABLE IF EXISTS ships.positions_20260611"
    )


def test_bank_precedes_drop_for_same_day():
    day = date(2026, 6, 1)
    # Both reference the same day; maintenance runs bank then drop in one txn.
    # The bank reads the parent table by range (no child partition name) so a
    # retry after the drop reads zero rows; the drop targets the child by name.
    assert "positions" in bank_day_sql(day, 0.005, 0.0075, 1.0)
    assert "positions_20260601" not in bank_day_sql(day, 0.005, 0.0075, 1.0)
    assert "positions_20260601" in drop_partition_sql(day)


def _days(n: int):
    from datetime import timedelta

    return timedelta(days=n)
