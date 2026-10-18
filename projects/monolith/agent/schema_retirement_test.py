"""Real PostgreSQL coverage for the fixed, metadata-only inventory."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from agent import schema_retirement as inventory


@pytest.fixture
def catalog(pg, monkeypatch):
    admin = create_engine(pg.url, isolation_level="AUTOCOMMIT")
    name = "schema_inventory_" + uuid4().hex
    with admin.connect() as conn:
        conn.exec_driver_sql(f"CREATE DATABASE {name}")
    engine = create_engine(
        admin.url.set(database=name), isolation_level="READ COMMITTED"
    )
    with engine.begin() as conn:
        conn.exec_driver_sql("""
            CREATE SCHEMA demo; CREATE SCHEMA semgrep; CREATE SCHEMA home;
            CREATE SCHEMA claude_agent; CREATE SCHEMA observability;
            CREATE SCHEMA public_api; CREATE SCHEMA knowledge;
            CREATE SCHEMA chat; CREATE SCHEMA agent_sessions; CREATE SCHEMA dr_jobs;
            CREATE TABLE demo.load_scan (id bigserial PRIMARY KEY);
            CREATE VIEW home.first_view AS SELECT id FROM demo.load_scan;
            CREATE VIEW home.second_view AS SELECT id FROM home.first_view;
            CREATE TABLE home.default_consumer (id bigint DEFAULT nextval('demo.load_scan_id_seq'));
            CREATE TABLE home.type_consumer (value demo.load_scan);
            CREATE TYPE semgrep.unexpected AS ENUM ('PRIVATE_ENUM_VALUE');
            CREATE TABLE home.tasks (id integer NOT NULL, payload text DEFAULT 'PRIVATE_DEFAULT');
            INSERT INTO home.tasks VALUES (7, 'PRIVATE_APPLICATION_ROW');
            CREATE FUNCTION claude_agent.notify_thread_pending(integer) RETURNS text
                LANGUAGE sql AS 'SELECT ''PRIVATE_FUNCTION_BODY''';
        """)
        # The migrated harness already has this role; a standalone local harness
        # may not. Test setup only, never the inventory's production path.
        conn.exec_driver_sql("""DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'public_reader')
            THEN CREATE ROLE public_reader; END IF;
            END $$;
            GRANT USAGE ON SCHEMA home TO public_reader;
            GRANT SELECT ON home.tasks TO public_reader;
        """)
    monkeypatch.setattr(inventory, "get_engine", lambda: engine)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.exec_driver_sql(f"DROP DATABASE {name}")
        admin.dispose()


def test_inventory_reports_absence_signatures_privileges_and_recursive_consumers(
    catalog,
):
    result = inventory.read_inventory()
    assert result["complete"] is True
    assert "PRIVATE_" not in json.dumps(result)
    data = result["inventory"]
    assert data["observation"][0]["read_only"] == "on"
    assert data["observation"][0]["isolation"] == "repeatable read"
    assert data["observation"][0]["is_replica"] is False
    relations = {r["name"]: r for r in data["relations"]}
    assert len(relations) == len(inventory.RETIRED) + len(inventory.RETAINED)
    assert relations["home.tasks"]["public_reader_select"]
    assert relations["home.tasks"]["public_reader_usage"]
    assert relations["public_api.agent_activity_snapshot"]["present"] is False
    assert (
        next(r for r in data["special_objects"] if r["kind"] == "function")["oid"]
        is None
    )
    assert any(
        r["attname"] == "id" and r["attnotnull"] for r in data["retained_columns"]
    )
    closure = {r["identity"] for r in data["dependency_closure"]}
    assert {
        "demo.load_scan_id_seq",
        "home.first_view",
        "home.second_view",
        "home.default_consumer",
        "home.type_consumer",
        "semgrep.unexpected",
    } <= closure
    assert not any("notify_thread_pending" in s for s in closure)
    assert any(r["identity"] == "semgrep.unexpected" for r in data["namespace_members"])
    with catalog.connect() as conn:
        assert (
            conn.execute(text("SELECT payload FROM home.tasks")).scalar_one()
            == "PRIVATE_APPLICATION_ROW"
        )


def test_snapshot_transaction_rejects_writes(catalog):
    with catalog.connect() as conn:
        inventory._inventory(conn)
        with pytest.raises(DBAPIError) as error:
            conn.execute(text("INSERT INTO home.tasks VALUES (8, 'forbidden')"))
        assert error.value.orig.sqlstate == "25006"


def test_exact_function_trigger_and_changed_effective_privilege(catalog):
    with catalog.begin() as conn:
        conn.exec_driver_sql("""
            CREATE TABLE claude_agent.agent_threads (id integer);
            CREATE FUNCTION claude_agent.notify_thread_pending() RETURNS trigger
                LANGUAGE plpgsql AS 'BEGIN RETURN NEW; END';
            CREATE TRIGGER agent_threads_pending_notify AFTER INSERT
                ON claude_agent.agent_threads FOR EACH ROW
                EXECUTE FUNCTION claude_agent.notify_thread_pending();
            REVOKE SELECT ON home.tasks FROM public_reader;
        """)
    result = inventory.read_inventory()
    assert result["complete"] is True
    data = result["inventory"]
    assert all(row["oid"] is not None for row in data["special_objects"])
    assert any(
        row["identity"] == "claude_agent.notify_thread_pending()"
        for row in data["dependency_closure"]
    )
    assert not next(row for row in data["relations"] if row["name"] == "home.tasks")[
        "public_reader_select"
    ]


@pytest.mark.parametrize(
    "limit,value,reason", [("MAX_ROWS", 2, "row_limit"), ("MAX_BYTES", 8, "byte_limit")]
)
def test_truncation_cannot_look_complete(catalog, monkeypatch, limit, value, reason):
    monkeypatch.setattr(inventory, limit, value)
    result = inventory.read_inventory()
    assert result == {
        "complete": False,
        "manifest": inventory.MANIFEST,
        "implementation_sha256": inventory.IMPLEMENTATION_SHA256,
        "reason": reason,
    }


def test_database_failure_does_not_export_exception_or_partial_inventory(
    catalog, monkeypatch
):
    monkeypatch.setattr(
        inventory, "QUERIES", {**inventory.QUERIES, "broken": "SELECT 1 / 0"}
    )
    result = inventory.read_inventory()
    assert result["complete"] is False
    assert result["reason"] == "catalog_read_failed"
    assert "inventory" not in result
