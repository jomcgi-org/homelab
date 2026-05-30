"""Per-entity monotonic event versioning (ADR agents/017).

``event_version`` is monotonic per ``(entity_type, entity_id)`` and is the
producer's responsibility (ADR 017). This module provides an atomic upsert
allocator backed by a single counter table. It accepts any PEP 249 DB-API
connection — the production path uses Postgres; tests use in-memory sqlite3.

The ``lakehouse_event_versions`` table is created by a **deferred migration**
(not in this unit). :data:`CREATE_TABLE_SQL` is exported so tests (and that
later migration) can stand the table up; production code must not create it
ad hoc.
"""

from __future__ import annotations

from typing import Any

# DDL for the counter table. The composite primary key gives one row per
# entity instance; ``version`` is the highest version allocated so far.
# Plain ANSI types so the same DDL works on both Postgres and sqlite3.
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS lakehouse_event_versions (
    entity_type text NOT NULL,
    entity_id   text NOT NULL,
    version     int  NOT NULL,
    PRIMARY KEY (entity_type, entity_id)
)
""".strip()

# Atomic allocate-next: insert v1 on first sight, otherwise bump the existing
# counter, returning the value assigned to this call. ON CONFLICT ... RETURNING
# is supported by both Postgres and modern sqlite3 (>= 3.35), so a single
# statement is race-free under either backend.
_UPSERT_SQL = """
INSERT INTO lakehouse_event_versions (entity_type, entity_id, version)
VALUES (?, ?, 1)
ON CONFLICT (entity_type, entity_id)
DO UPDATE SET version = lakehouse_event_versions.version + 1
RETURNING version
""".strip()


def next_event_version(conn: Any, entity_type: str, entity_id: str) -> int:
    """Allocate and return the next monotonic version for an entity.

    First call for a given ``(entity_type, entity_id)`` returns ``1``; each
    subsequent call returns the prior value plus one. Distinct entities have
    independent counters. The allocation is a single atomic upsert, so two
    concurrent producers cannot be handed the same version.

    ``conn`` is a DB-API connection (e.g. ``sqlite3.Connection`` or a psycopg
    connection). The caller owns the surrounding transaction; this function
    does not commit so the version allocation can join the producer's
    event-generation transaction (ADR 017: transactional event generation
    guarantees order).
    """
    cur = conn.cursor()
    try:
        cur.execute(_UPSERT_SQL, (entity_type, entity_id))
        row = cur.fetchone()
        if row is None:  # pragma: no cover - defensive; RETURNING always yields a row
            raise RuntimeError(
                "next_event_version: upsert returned no row; backend may not "
                "support INSERT ... ON CONFLICT ... RETURNING"
            )
        return int(row[0])
    finally:
        cur.close()
