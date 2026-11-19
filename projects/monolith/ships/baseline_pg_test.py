"""The baseline's ships.positions query against the hub migrations."""

from datetime import date, datetime, timezone

from sqlalchemy import text
from sqlmodel import create_engine

from ships import baseline
from ships.retention import create_partition_sql

DAY = date(2026, 11, 20)
# The test PostgreSQL has no zone files. It resolves GMT without them, and the
# partition bounds need a UTC session.
UTC_SESSION = {"options": "-c timezone=GMT"}


def test_measure_positions_counts_rows_and_bytes_per_partition(pg):
    engine = create_engine(pg.url, connect_args=UTC_SESSION)
    try:
        with engine.begin() as conn:
            conn.execute(text(create_partition_sql(DAY)))
            conn.execute(
                text(
                    "INSERT INTO ships.positions (mmsi, lat, lon, recorded_at)"
                    " SELECT 'm' || n, 48.0, -125.0, :day + n * interval '1 minute'"
                    " FROM generate_series(1, 30) AS n"
                ),
                {"day": datetime(2026, 11, 20, tzinfo=timezone.utc)},
            )
            conn.execute(
                text(
                    "INSERT INTO ships.positions (mmsi, lat, lon, recorded_at)"
                    " VALUES ('old', 48.0, -125.0, '2026-01-01T00:00:00Z')"
                )
            )
        at = datetime(2026, 11, 21, 0, 5, tzinfo=timezone.utc)

        result = baseline.measure_positions(engine, lambda: at)
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE ships.positions_20261120"))
            conn.execute(text("DELETE FROM ships.positions"))
        engine.dispose()

    by_name = {p["name"]: p for p in result["partitions"]}
    assert by_name["ships.positions_20261120"]["rows"] == 30
    assert by_name["ships.positions_default"]["rows"] == 1
    assert result["rows"] == 31
    assert result["partition_bytes"] == sum(
        p["partition_bytes"] for p in result["partitions"]
    )
    assert by_name["ships.positions_20261120"]["partition_bytes"] > 0
    assert by_name["ships.positions_20261120"]["index_bytes"] > 0
    assert result["observed_at"] == "2026-11-21T00:05:00Z"
    assert result["source"] == baseline.POSITIONS_SOURCE
