"""Real-Postgres contract for the observability snapshots (ADR 004 Layer 4).

Asserts, against a real Postgres (the `pg` fixture applies every migration):
  - the single-row upsert (same SQL the rollup writer runs) is idempotent, and
  - public_reader can read the payload snapshots and merged PR snapshot through
    their table-specific grants.

Hand-written bdd_test (real DB), so excluded from gazelle. The rollup writer's
build->write wiring is covered separately in home/observability/rollup_test.py.
"""

import json

from sqlmodel import Session, create_engine, text

_UPSERT_TOPOLOGY = text(
    """
    INSERT INTO observability.topology_snapshot (id, payload, snapshot_at)
    VALUES (1, :payload, now())
    ON CONFLICT (id) DO UPDATE
        SET payload = EXCLUDED.payload, snapshot_at = EXCLUDED.snapshot_at
    """
)
_UPSERT_STATS = text(
    """
    INSERT INTO observability.stats_snapshot (id, payload, snapshot_at)
    VALUES (1, :payload, now())
    ON CONFLICT (id) DO UPDATE
        SET payload = EXCLUDED.payload, snapshot_at = EXCLUDED.snapshot_at
    """
)


def test_snapshot_upsert_idempotent_and_public_reader_can_read(pg):
    engine = create_engine(pg.url)
    try:
        with Session(engine) as session:
            session.execute(
                _UPSERT_TOPOLOGY,
                {
                    "payload": json.dumps(
                        {"nodes": [{"id": "a"}], "groups": [], "edges": []}
                    )
                },
            )
            # Second upsert on the singleton row: last write wins, no duplicate.
            session.execute(
                _UPSERT_TOPOLOGY,
                {
                    "payload": json.dumps(
                        {"nodes": [{"id": "b"}], "groups": [], "edges": []}
                    )
                },
            )
            session.execute(
                _UPSERT_STATS, {"payload": json.dumps({"cluster": {"nodes": 4}})}
            )
            session.execute(
                text(
                    """
                    INSERT INTO observability.merged_prs
                        (number, title, merged_at, additions, deletions,
                         changed_files, type, scope, agent_authored)
                    VALUES
                        (5898, 'feat(observability): snapshot merged prs', now(),
                         10, 2, 3, 'feat', 'observability', true)
                    """
                )
            )
            session.commit()

            count = session.execute(
                text("SELECT count(*) FROM observability.topology_snapshot")
            ).scalar_one()
            assert count == 1

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
            merged_pr = session.execute(
                text("SELECT title FROM observability.merged_prs WHERE number = 5898")
            ).first()
            assert merged_pr[0] == "feat(observability): snapshot merged prs"
    finally:
        engine.dispose()
