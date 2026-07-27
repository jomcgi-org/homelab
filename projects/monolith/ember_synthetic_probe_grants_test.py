"""Real-Postgres contract for ember_synthetic_probe public grants.

Asserts, against a real Postgres (the `pg` fixture applies every migration,
including the one creating ember_synthetic_probe and granting SELECT to
public_reader):
  - public_reader can SELECT from ember_synthetic_probe (the read path the
    public tier's /api/health uses to detect probe timeouts and report service
    health), and
  - public_reader cannot INSERT into it, keeping the read role from becoming
    a writer.

Unlike the demo_pg_savings grants test, there is no public_writer grant here:
the writer is the app role (the ember-synthetic CronWorkflows jobs pod), not
the public tier, so no public_writer test is needed.

Hand-written bdd_test (real DB), so excluded from gazelle and registered by
hand in projects/monolith/BUILD, mirroring ember_public_savings_grants_test.py.
"""

import pytest
from sqlmodel import Session, create_engine, text


def test_public_reader_can_select_ember_synthetic_probe(pg):
    engine = create_engine(pg.url)
    try:
        with Session(engine) as session:
            session.execute(text("SET ROLE public_reader"))

            # SELECT is granted: must not raise.
            session.execute(
                text("SELECT count(*) FROM ember_synthetic_probe")
            ).scalar_one()

            # Never commit: the test session is rolled back so other tests stay clean.
            session.rollback()
    finally:
        engine.dispose()


def test_public_reader_cannot_write_ember_synthetic_probe(pg):
    engine = create_engine(pg.url)
    try:
        with Session(engine) as session:
            session.execute(text("SET ROLE public_reader"))

            # INSERT must be denied.
            with pytest.raises(Exception) as exc:
                session.execute(
                    text(
                        "INSERT INTO ember_synthetic_probe (demo, ok, checked_at) "
                        "VALUES ('test-demo', true, NOW())"
                    )
                )
            assert "permission denied" in str(exc.value).lower()
            session.rollback()
    finally:
        engine.dispose()
