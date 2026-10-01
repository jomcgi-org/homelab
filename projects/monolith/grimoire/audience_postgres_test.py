"""Execute the same audience agreement matrix on the real BDD PostgreSQL."""

from uuid import uuid4

from sqlalchemy import create_engine

from grimoire.testing.audience_matrix import assert_agreement, audience_table


def test_postgres_audience_agreement(pg):
    engine = create_engine(pg.url)
    table = audience_table(f"audience_matrix_{uuid4().hex}")
    try:
        table.create(engine)
        with engine.begin() as connection:
            assert_agreement(connection, table)
    finally:
        table.drop(engine, checkfirst=True)
        engine.dispose()
