"""Real-Postgres round-trip for the observability snapshots (ADR 004 Layer 4).

Exercises the rollup writer's upsert against a real Postgres (the `pg` fixture
applies every migration) and asserts public_reader can read the snapshot tables
through the grants in 20260617010000_observability_snapshots.sql. Hand-written
bdd_test (real DB), so excluded from gazelle.
"""

import os

from sqlmodel import Session, create_engine, text

from app.db import get_engine
from home.observability.rollup import _write_stats_snapshot, _write_topology_snapshot


def test_rollup_writer_upserts_and_public_reader_can_read(pg):
    # Point the app engine (used by the writers) at the test Postgres.
    os.environ["DATABASE_URL"] = pg.url.replace(
        "postgresql+psycopg://", "postgresql://", 1
    )
    get_engine.cache_clear()
    engine = create_engine(pg.url)
    try:
        # The writer is an idempotent single-row upsert: last write wins.
        _write_topology_snapshot({"nodes": [{"id": "a"}], "groups": [], "edges": []})
        _write_topology_snapshot({"nodes": [{"id": "b"}], "groups": [], "edges": []})
        _write_stats_snapshot({"cluster": {"nodes": 4}})

        with Session(engine) as session:
            row = session.execute(
                text("SELECT payload FROM observability.topology_snapshot WHERE id = 1")
            ).first()
            assert row[0]["nodes"][0]["id"] == "b"

        # public_reader (Phase 2 role) can read both snapshot tables.
        with Session(engine) as session:
            session.execute(text("SET ROLE public_reader"))
            topo = session.execute(
                text("SELECT payload FROM observability.topology_snapshot WHERE id = 1")
            ).first()
            assert topo[0]["nodes"][0]["id"] == "b"
            stats = session.execute(
                text("SELECT payload FROM observability.stats_snapshot WHERE id = 1")
            ).first()
            assert stats[0]["cluster"]["nodes"] == 4
    finally:
        engine.dispose()
        get_engine.cache_clear()
