"""The agent snapshot grant does not open the private dashboard schema."""

import pytest
from sqlalchemy.exc import ProgrammingError
from sqlmodel import Session, create_engine, text


def test_agents_writer_can_read_cluster_snapshot(pg):
    engine = create_engine(pg.url)
    try:
        with Session(engine) as session:
            session.execute(
                text(
                    "INSERT INTO agent_view.cluster_snapshot (payload) "
                    "VALUES ('{\"schema_version\": 1}'::jsonb) "
                    "ON CONFLICT (id) DO UPDATE SET payload = EXCLUDED.payload"
                )
            )
            session.commit()
            session.execute(text("SET ROLE agents_writer"))
            payload = session.execute(
                text("SELECT payload FROM agent_view.cluster_snapshot WHERE id = 1")
            ).scalar_one()
            assert payload == {"schema_version": 1}
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO agent_view.cluster_snapshot (payload) VALUES ('{}'::jsonb)",
        "SELECT * FROM home.cluster_snapshot",
    ],
)
def test_agents_writer_denied_snapshot_writes_and_private_reads(pg, statement):
    engine = create_engine(pg.url)
    try:
        with Session(engine) as session:
            session.execute(text("SET ROLE agents_writer"))
            with pytest.raises(ProgrammingError, match="permission denied"):
                session.execute(text(statement))
    finally:
        engine.dispose()
