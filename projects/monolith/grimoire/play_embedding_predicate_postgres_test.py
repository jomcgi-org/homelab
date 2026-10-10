"""Real PostgreSQL JSONB membership runs the same complete source oracle."""

from uuid import uuid4

from sqlalchemy import create_engine

from grimoire import play_embeddings
from grimoire.testing.play_embedding_matrix import assert_play_agreement, matrix_tables


def test_postgres_play_embedding_source_agreement(pg, monkeypatch):
    engine = create_engine(pg.url)
    embeddings, events, notes = matrix_tables("play_" + uuid4().hex)
    try:
        for table in (embeddings, events, notes):
            table.create(engine)
        monkeypatch.setattr(play_embeddings, "Embedding", embeddings.c)
        with engine.begin() as connection:
            assert_play_agreement(
                connection,
                embeddings,
                events,
                notes,
                play_embeddings.play_embedding_predicate,
            )
    finally:
        for table in (embeddings, events, notes):
            table.drop(engine, checkfirst=True)
        engine.dispose()
