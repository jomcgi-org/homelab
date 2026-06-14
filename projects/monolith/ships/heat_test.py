"""Unit tests for ships/heat.py: the traffic-density rollup SQL builder.

The pure SQL builder is the testable unit; the live DELETE+INSERT is Postgres-
only (aggregation over ships.positions) and is exercised in prod / CI-against-
Postgres, never here.
"""

from datetime import date

from ships.heat import bank_day_sql, rollup_insert_sql


def test_rollup_insert_sql_counts_distinct_movers():
    sql = rollup_insert_sql(0.005, 0.0075, 1.0, 7)

    # Full-replace destination is the rollup table.
    assert "INSERT INTO ships.heat_cells (lat_bin, lon_bin, count)" in sql
    # Distinct vessels (not raw fixes) so dwell can't dominate.
    assert "count(distinct p.mmsi)" in sql
    # Only vessels that move at some point in the window (per-vessel filter).
    assert "GROUP BY mmsi HAVING max(speed) >= 1.0" in sql
    # Trailing window is applied to both the movers CTE and the main scan.
    assert sql.count("interval '7 days'") == 2
    # Cell binning uses the supplied steps.
    assert "floor(p.lat / 0.005)::int" in sql
    assert "floor(p.lon / 0.0075)::int" in sql
    # Bogus coordinates are dropped.
    assert "AND p.lat BETWEEN -90 AND 90 AND p.lon BETWEEN -180 AND 180" in sql
    assert "NOT (p.lat = 0 AND p.lon = 0)" in sql


def test_rollup_insert_sql_threads_all_params():
    sql = rollup_insert_sql(0.01, 0.02, 2.5, 3)
    assert "floor(p.lat / 0.01)::int" in sql
    assert "floor(p.lon / 0.02)::int" in sql
    assert "HAVING max(speed) >= 2.5" in sql
    assert "interval '3 days'" in sql


def test_bank_day_sql_targets_historical_and_filters_one_day():
    sql = bank_day_sql(date(2026, 6, 1), 0.005, 0.0075, 1.0)
    assert "INSERT INTO ships.heat_cells_historical" in sql
    assert "ON CONFLICT (lat_bin, lon_bin)" in sql
    assert "count = ships.heat_cells_historical.count + EXCLUDED.count" in sql
    assert "FROM ships.positions" in sql
    assert "positions_20260601" not in sql
    assert "recorded_at >= '2026-06-01'" in sql
    assert "recorded_at < '2026-06-02'" in sql
    assert "max(speed) >= 1.0" in sql
    assert "count(distinct" in sql
