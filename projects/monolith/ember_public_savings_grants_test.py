"""Real-Postgres contract for demo_pg_savings public grants (Task 3, ADR
security/005's narrow-grant precedent).

Asserts, against a real Postgres (the `pg` fixture applies every migration,
including 20260718010000_demo_pg_savings_public_grants.sql):
  - public_writer can SELECT, INSERT, and UPDATE demo_pg_savings (the accrual
    path used by both tiers' status poll), and
  - public_reader can SELECT it (the cached GET /savings endpoint) but
    cannot write it, keeping the read role from becoming a writer.

Hand-written bdd_test (real DB), so excluded from gazelle and registered by
hand in projects/monolith/BUILD, mirroring chat_public_grants_test.py.
"""

import pytest
from sqlmodel import Session, create_engine, text


def test_public_writer_can_select_insert_update_demo_pg_savings(pg):
    engine = create_engine(pg.url)
    try:
        with Session(engine) as session:
            session.execute(text("SET ROLE public_writer"))

            session.execute(
                text(
                    "INSERT INTO demo_pg_savings (id, total_mib_seconds) "
                    "VALUES (1, 0) ON CONFLICT (id) DO NOTHING"
                )
            )
            session.execute(
                text("UPDATE demo_pg_savings SET total_mib_seconds = 512 WHERE id = 1")
            )
            total = session.execute(
                text("SELECT total_mib_seconds FROM demo_pg_savings WHERE id = 1")
            ).scalar_one()
            assert total == 512

            # Never commit: the row is rolled back so other tests stay clean.
            session.rollback()
    finally:
        engine.dispose()


def test_public_reader_can_select_but_not_write_demo_pg_savings(pg):
    engine = create_engine(pg.url)
    try:
        with Session(engine) as session:
            session.execute(text("SET ROLE public_reader"))

            # SELECT is granted: must not raise.
            session.execute(text("SELECT count(*) FROM demo_pg_savings")).scalar_one()

            with pytest.raises(Exception) as exc:
                session.execute(
                    text(
                        "INSERT INTO demo_pg_savings (id, total_mib_seconds) "
                        "VALUES (1, 0) ON CONFLICT (id) DO NOTHING"
                    )
                )
            assert "permission denied" in str(exc.value).lower()
            session.rollback()
    finally:
        engine.dispose()
