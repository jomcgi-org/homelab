"""Real PostgreSQL contracts for the #7030 maintenance-window transition."""

import io
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from hikes.tools.generate_seed import COLUMNS, generate
from shared.testing.plugin import _find_migrations_dir
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlmodel import Session, create_engine, text

from home import cluster_snapshot


@pytest.fixture
def snapshot_db(pg):
    engine = create_engine(pg.url)
    try:
        with engine.connect() as conn, conn.begin():
            yield conn
            conn.rollback()
    finally:
        engine.dispose()


def _retire(conn):
    conn.execute(
        text(
            (
                _find_migrations_dir() / "20261029090000_home_drop_snapshot_alerts.sql"
            ).read_text()
        )
    )


def _restore_old_table(conn):
    conn.exec_driver_sql("DROP TABLE home.cluster_snapshot")
    conn.execute(
        text(
            (
                _find_migrations_dir() / "20260712000000_home_cluster_snapshot.sql"
            ).read_text()
        )
    )


def _runtime_write(conn, health):
    # Drive the actual writer SQL. Its commit releases a savepoint, leaving the
    # outer transaction isolated for this test.
    with patch("core.db.get_engine", return_value=conn):
        with patch.object(
            cluster_snapshot,
            "Session",
            lambda bind: Session(bind=bind, join_transaction_mode="create_savepoint"),
        ):
            cluster_snapshot._write_cluster_snapshot(health)


def test_old_schema_rejects_new_writer_until_migrated(snapshot_db):
    conn = snapshot_db
    _restore_old_table(conn)
    with pytest.raises(IntegrityError) as error:
        with conn.begin_nested():
            _runtime_write(conn, {"healthy": True})
    assert error.value.orig.sqlstate == "23502"
    assert error.value.orig.diag.column_name == "alerts"


def test_retirement_preserves_row_and_agent_snapshot_and_new_sql_works(snapshot_db):
    conn = snapshot_db
    _restore_old_table(conn)
    conn.exec_driver_sql(
        "INSERT INTO ships.vessels (mmsi, imo, call_sign, dimension_a, dimension_b, "
        "dimension_c, dimension_d, draught) VALUES ('7030', 'keep-imo', 'keep-call', 1, 2, 3, 4, 5.5);"
        "INSERT INTO campsites.campgrounds (resource_location_id, park_map_id, name, "
        "latitude, longitude, description) VALUES (7030, 42, 'Retained', 49, -123, 'Keep description');"
        "INSERT INTO campsites.availability (resource_location_id, date, loops_open) "
        "VALUES (7030, '2026-10-29', 3);"
        "INSERT INTO campsites.weather (resource_location_id, date, precip_prob, wind_max) "
        "VALUES (7030, '2026-10-29', 25, 12.5)"
    )
    retained_tables = (
        "ships.vessels",
        "campsites.campgrounds",
        "campsites.availability",
        "campsites.weather",
    )
    retained_before = {
        table: conn.exec_driver_sql(f"SELECT * FROM {table}").all()
        for table in retained_tables
    }
    health = {"healthy": False, "scanned": 7, "unhealthy": {"pods": [{"name": "x"}]}}
    conn.execute(
        text(
            "INSERT INTO home.cluster_snapshot (health, alerts) VALUES (:health, :alerts)"
        ),
        {"health": json.dumps(health), "alerts": '{"legacy": true}'},
    )
    conn.exec_driver_sql(
        "INSERT INTO agent_view.cluster_snapshot (id, payload) VALUES (1, '{\"keep\":true}') "
        "ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload"
    )
    before = conn.exec_driver_sql(
        "SELECT id, health, snapshot_at FROM home.cluster_snapshot"
    ).one()
    agent_before = conn.exec_driver_sql(
        "SELECT * FROM agent_view.cluster_snapshot"
    ).all()
    _retire(conn)
    for table, rows in retained_before.items():
        assert conn.exec_driver_sql(f"SELECT * FROM {table}").all() == rows
    assert (
        conn.exec_driver_sql(
            "SELECT id, health, snapshot_at FROM home.cluster_snapshot"
        ).one()
        == before
    )
    assert (
        conn.exec_driver_sql("SELECT * FROM agent_view.cluster_snapshot").all()
        == agent_before
    )
    with Session(bind=conn, join_transaction_mode="create_savepoint") as session:
        snap = cluster_snapshot.read_cluster_snapshot(session)
        assert snap["health"] == health
        assert set(snap) == {"health", "snapshot_at", "age_secs"}
    _runtime_write(conn, {"healthy": True, "scanned": 8})
    _runtime_write(conn, health)
    assert conn.exec_driver_sql(
        "SELECT id, health FROM home.cluster_snapshot"
    ).all() == [(1, health)]
    _retire(conn)
    assert (
        conn.exec_driver_sql("SELECT * FROM agent_view.cluster_snapshot").all()
        == agent_before
    )
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            conn.exec_driver_sql(
                "INSERT INTO home.cluster_snapshot (id, health) VALUES (2, '{}')"
            )


@pytest.mark.parametrize(
    "old_sql",
    [
        "SELECT health, alerts, snapshot_at FROM home.cluster_snapshot WHERE id = 1",
        "INSERT INTO home.cluster_snapshot (id, health, alerts) VALUES (1, '{}', '{}') "
        "ON CONFLICT (id) DO UPDATE SET alerts = EXCLUDED.alerts",
    ],
)
def test_old_consumers_fail_after_retirement(snapshot_db, old_sql):
    with pytest.raises(ProgrammingError) as error:
        with snapshot_db.begin_nested():
            snapshot_db.exec_driver_sql(old_sql)
    assert error.value.orig.sqlstate == "42703"


def test_dependent_view_refuses_drop_without_data_loss(snapshot_db):
    conn = snapshot_db
    _restore_old_table(conn)
    conn.exec_driver_sql(
        "INSERT INTO home.cluster_snapshot (health, alerts) VALUES ('{}', '{\"keep\":true}')"
    )
    conn.exec_driver_sql(
        "CREATE VIEW home.alerts_dependency AS SELECT alerts FROM home.cluster_snapshot"
    )
    before = conn.exec_driver_sql("SELECT * FROM home.cluster_snapshot").all()
    with pytest.raises(DBAPIError) as error:
        with conn.begin_nested():
            _retire(conn)
    assert error.value.orig.sqlstate == "2BP01"
    assert conn.exec_driver_sql("SELECT * FROM home.cluster_snapshot").all() == before


def test_real_missing_table_falls_back_but_missing_column_propagates(snapshot_db):
    conn = snapshot_db
    conn.exec_driver_sql("DROP TABLE home.cluster_snapshot")
    with Session(bind=conn, join_transaction_mode="create_savepoint") as session:
        assert cluster_snapshot.read_cluster_snapshot(session) is None
        assert session.execute(text("SELECT 1")).scalar() == 1
    conn.exec_driver_sql("CREATE TABLE home.cluster_snapshot (id smallint PRIMARY KEY)")
    with Session(bind=conn, join_transaction_mode="create_savepoint") as session:
        with pytest.raises(ProgrammingError):
            cluster_snapshot.read_cluster_snapshot(session)
        assert session.execute(text("SELECT 1")).scalar() == 1


def test_real_empty_and_stale_rows_fall_back(snapshot_db):
    conn = snapshot_db
    conn.exec_driver_sql("DELETE FROM home.cluster_snapshot")
    with Session(bind=conn, join_transaction_mode="create_savepoint") as session:
        assert cluster_snapshot.read_cluster_snapshot(session) is None
        session.execute(
            text(
                "INSERT INTO home.cluster_snapshot (health, snapshot_at) VALUES ('{}', now() - interval '1 hour')"
            )
        )
        assert cluster_snapshot.read_cluster_snapshot(session) is None


def test_retained_seed_loads_current_schema_and_repeats_without_overwrite(snapshot_db):
    conn = snapshot_db
    conn.exec_driver_sql("DELETE FROM hikes.walk_hours; DELETE FROM hikes.walks")
    seed = Path(__file__).resolve().parents[1] / "hikes" / "seed" / "walks_seed.sql"
    conn.execute(text(seed.read_text()))
    before = conn.exec_driver_sql("SELECT * FROM hikes.walks ORDER BY uuid").all()
    assert len(before) == 1620
    uuid = before[0].uuid
    stamp = datetime(2026, 10, 29, tzinfo=timezone.utc)
    conn.execute(
        text(
            "UPDATE hikes.walks SET name = 'Recovered and refreshed', forecast_fetched_at = :stamp WHERE uuid = :uuid"
        ),
        {"uuid": uuid, "stamp": stamp},
    )
    updated = conn.exec_driver_sql("SELECT * FROM hikes.walks ORDER BY uuid").all()
    conn.execute(text(seed.read_text()))
    assert (
        conn.exec_driver_sql("SELECT * FROM hikes.walks ORDER BY uuid").all() == updated
    )


def test_generated_seed_from_sqlite_loads_current_schema(snapshot_db, tmp_path):
    db = tmp_path / "recovery.db"
    with sqlite3.connect(db) as sqlite:
        sqlite.execute("CREATE TABLE walks (" + ", ".join(COLUMNS) + ")")
        sqlite.executemany(
            "INSERT INTO walks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "recovery-fixture",
                    "Beinn a' Chlair",
                    "https://example.invalid/walk",
                    10.5,
                    500,
                    4.0,
                    None,
                    57.0,
                    -4.0,
                ),
                (
                    "recovery-skip",
                    None,
                    "https://example.invalid/skip",
                    1,
                    1,
                    1,
                    "",
                    57,
                    -4,
                ),
            ],
        )
    sql = io.StringIO()
    assert generate(db, sql) == (1, 1)
    snapshot_db.execute(text(sql.getvalue()))
    assert snapshot_db.exec_driver_sql(
        "SELECT name, summary, distance_km, ascent_m FROM hikes.walks WHERE uuid = 'recovery-fixture'"
    ).one() == ("Beinn a' Chlair", "", 10.5, 500)
    snapshot_db.exec_driver_sql(
        "UPDATE hikes.walks SET name = 'Updated' WHERE uuid = 'recovery-fixture'"
    )
    snapshot_db.execute(text(sql.getvalue()))
    assert (
        snapshot_db.exec_driver_sql(
            "SELECT name FROM hikes.walks WHERE uuid = 'recovery-fixture'"
        ).scalar()
        == "Updated"
    )
