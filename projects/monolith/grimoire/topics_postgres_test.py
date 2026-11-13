"""Migrated PostgreSQL text/UUID and pgvector predicates, with no model calls."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, create_engine

from grimoire.ingest import (
    _upsert_book,
    _upsert_book_chunks,
    parse_manifest_lines,
    upsert_embedding_batch,
)
from grimoire.models import KnowledgeChunk
from grimoire.search import _resolve_chunk_hit, knn_embeddings
from grimoire.topics import classify_stored_chunks, topic_embedding_predicate


def test_migrated_topic_constraint_and_ranked_live_predicate(pg):
    # pg applies every real migration, including the additive topic column.
    engine = create_engine(pg.url)
    book = f"synthetic-dmg-{uuid4()}"
    with Session(engine) as session:
        _upsert_book(session, book)
        manifests = [
            {
                "chunk_ref": f"mundane-{i:03d}",
                "content": "The gate weighs a hundred pounds.",
                "section_path": "Running the Game",
            }
            for i in range(60)
        ] + [
            {
                "chunk_ref": "guidance",
                "content": "You can ask your players which encounters interest their characters.",
                "section_path": "Getting Players Invested",
            }
        ]
        parsed, errors = parse_manifest_lines(book, map(json.dumps, manifests))
        assert not errors
        rows, count = _upsert_book_chunks(session, book, parsed)
        assert count == 61
        advice = rows[-1]
        advice_id = advice.id
        assert advice.topic == "dm_guidance"
        query = [1.0] + [0.0] * 1023
        # Sixty irrelevant vectors are closer than the one eligible chunk.
        vectors = [query for _ in range(60)] + [[0.8, 0.6] + [0.0] * 1022]
        upsert_embedding_batch(session, "synthetic-test", "chunk", rows, vectors)
        assert advice_id not in {
            hit.embeddable_id
            for hit, _ in knn_embeddings(
                session, query, ("chunk",), 4, model="synthetic-test"
            )
        }
        predicate = topic_embedding_predicate("dm_guidance")
        hits = knn_embeddings(
            session, query, ("chunk",), 1, model="synthetic-test", where=predicate
        )
        assert [hit.embeddable_id for hit, _ in hits] == [advice_id]
        assert (
            _resolve_chunk_hit(session, advice_id, hits[0][1], "dm_guidance")["topic"]
            == "dm_guidance"
        )
        # The DB validates storage even for writers bypassing Python validation.
        with pytest.raises(IntegrityError), session.begin_nested():
            session.execute(
                text(
                    "UPDATE grimoire.knowledge_chunk SET topic = 'unknown' WHERE id = :id"
                ),
                {"id": advice_id},
            )
        assert session.get(KnowledgeChunk, advice_id).topic == "dm_guidance"
        # A stale vector cannot restore classification cleared by content refresh.
        manifests[-1]["content"] = "The gate is made of brass."
        parsed, _ = parse_manifest_lines(book, map(json.dumps, manifests))
        changed, _ = _upsert_book_chunks(session, book, parsed)
        assert [row.id for row in changed] == [advice_id]
        assert (
            knn_embeddings(
                session, query, ("chunk",), 1, model="synthetic-test", where=predicate
            )
            == []
        )
        assert _resolve_chunk_hit(session, advice_id, 0.0, "dm_guidance") is None
        # NULL supports old unclassified rows; bounded backfill uses real text type.
        session.execute(
            text("UPDATE grimoire.knowledge_chunk SET content = :body WHERE id = :id"),
            {
                "body": "You can ask your players which encounters interest their characters.",
                "id": advice_id,
            },
        )
        session.expire_all()
        page = classify_stored_chunks(session, book_id=book, limit=1)
        assert page == {"scanned": 1, "changed": 1, "next_ref": "guidance"}
        session.commit()
        assert classify_stored_chunks(session, book_id=book, limit=1)["changed"] == 0
        # Delete source while preserving the generic embedding row.
        session.execute(
            text("DELETE FROM grimoire.knowledge_chunk WHERE id = :id"),
            {"id": advice_id},
        )
        session.commit()
        assert (
            knn_embeddings(
                session, query, ("chunk",), 1, model="synthetic-test", where=predicate
            )
            == []
        )
        assert _resolve_chunk_hit(session, advice_id, 0.0, "dm_guidance") is None
    engine.dispose()
