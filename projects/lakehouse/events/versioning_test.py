"""Unit tests for per-entity monotonic event versioning (ADR agents/017).

Uses an in-memory sqlite3 DB, which supports ``INSERT ... ON CONFLICT ...
RETURNING`` (sqlite >= 3.35) — the same statement runs against Postgres in
production.
"""

from __future__ import annotations

import sqlite3

import pytest

from projects.lakehouse.events.versioning import CREATE_TABLE_SQL, next_event_version


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute(CREATE_TABLE_SQL)
    yield c
    c.close()


def test_first_call_returns_one(conn):
    assert next_event_version(conn, "gap", "gap-1") == 1


def test_monotonic_increment(conn):
    assert next_event_version(conn, "gap", "gap-1") == 1
    assert next_event_version(conn, "gap", "gap-1") == 2
    assert next_event_version(conn, "gap", "gap-1") == 3


def test_distinct_entities_independent(conn):
    assert next_event_version(conn, "gap", "gap-1") == 1
    assert next_event_version(conn, "gap", "gap-2") == 1
    assert next_event_version(conn, "gap", "gap-1") == 2
    assert next_event_version(conn, "gap", "gap-2") == 2


def test_distinct_entity_types_independent(conn):
    # Same entity_id under different entity_type are separate counters.
    assert next_event_version(conn, "gap", "x-1") == 1
    assert next_event_version(conn, "note", "x-1") == 1
    assert next_event_version(conn, "gap", "x-1") == 2


def test_create_table_sql_is_idempotent(conn):
    # Re-running the DDL must not error (IF NOT EXISTS).
    conn.execute(CREATE_TABLE_SQL)
    assert next_event_version(conn, "gap", "gap-1") == 1
