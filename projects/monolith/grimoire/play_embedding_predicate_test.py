"""Copied play audience predicate agrees with every SQLite source matrix case."""

import pytest
from sqlalchemy import create_engine

from grimoire import play_embeddings
from grimoire.testing.audience_matrix import MEMBERS
from grimoire.testing.play_embedding_matrix import assert_play_agreement, matrix_tables


def test_sqlite_play_embedding_source_agreement(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'audiences.db'}")
    embeddings, events, notes = matrix_tables("play_matrix")
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
        engine.dispose()


@pytest.mark.parametrize(
    "member,viewer", [(None, None), (MEMBERS[1], "dm"), (MEMBERS[0], "a")]
)
def test_embedding_predicate_rejects_nonmembers_and_inconsistent_viewers(
    member, viewer
):
    with pytest.raises(ValueError):
        play_embeddings.play_embedding_predicate("ours", viewer, member)
