"""Unit tests for grimoire_chat public-corpus retrieval.

The query embedding and the pgvector kNN are mocked (SQLite has no
cosine_distance operator, mirroring grimoire/search_test.py), so the focus is the
integration contract that matters for a public, adversarially-hardened surface:

1. Embedding-model-space match: retrieve() embeds with a client and passes THAT
   client's model to knn_embeddings, so the cosine search never mixes model
   spaces (a mismatched model returns garbage).
2. is_global filtering: a campaign-private entity hit (is_global false) is dropped;
   only the public corpus reaches the answer.
3. Chunk hits resolve to book+section-titled passages; entity hits resolve to a
   compact statblock.
4. Fail-open: a blank query embeds nothing, and an embedder error yields an
   ungrounded (empty) turn rather than an error.
5. Hybrid retrieval: a lexical entity-name match anchors a named entity the pure
   vector search ranked below its siblings, merged ahead of the vector hits, while
   the same is_global gate still drops any campaign-private name match.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from grimoire.models import Book, Entity, EntitySpell, KnowledgeChunk
from grimoire_chat import retrieval


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # SQLite can't span schemas, so strip the Postgres-only schema= overrides so
    # SQLModel.metadata.create_all() lands every table in the default schema.
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            yield session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


def _mock_client(vector, model="voyage-4-nano"):
    return SimpleNamespace(embed=AsyncMock(return_value=vector), model=model)


def _hit(kind, embeddable_id, distance):
    return (
        SimpleNamespace(embeddable_kind=kind, embeddable_id=embeddable_id),
        distance,
    )


# ---------------------------------------------------------------------------
# 1. Embedding-model-space match
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_passes_client_model_to_knn(session, monkeypatch):
    seen = {}

    def fake_knn(sess, vector, kinds, limit, model=None):
        seen["kinds"] = kinds
        seen["limit"] = limit
        seen["model"] = model
        return []

    monkeypatch.setattr(retrieval, "knn_embeddings", fake_knn)
    client = _mock_client([0.1] * 1024, model="voyage-4-nano")

    await retrieval.retrieve(
        session, "how does grappling work?", k=6, embed_client=client
    )

    client.embed.assert_awaited_once_with("how does grappling work?")
    # The model space of the stored corpus is passed to knn so the cosine search
    # is restricted to that model (never scored against a mismatched vector).
    assert seen["model"] == "voyage-4-nano"
    assert seen["kinds"] == ("chunk", "entity")
    assert seen["limit"] == 6 * retrieval.OVERFETCH_FACTOR


# ---------------------------------------------------------------------------
# 2. is_global filtering drops campaign-private entity hits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_drops_non_global_entity(session, monkeypatch):
    public = Entity(entity_type="creature", name="Goblin", is_global=True)
    private = Entity(entity_type="npc", name="Party Secret NPC", is_global=False)
    session.add(public)
    session.add(private)
    session.commit()
    session.refresh(public)
    session.refresh(private)

    def fake_knn(sess, vector, kinds, limit, model=None):
        return [_hit("entity", public.id, 0.1), _hit("entity", private.id, 0.2)]

    monkeypatch.setattr(retrieval, "knn_embeddings", fake_knn)
    client = _mock_client([0.1] * 1024)

    passages = await retrieval.retrieve(session, "a goblin", k=6, embed_client=client)

    ids = [p.ref_id for p in passages]
    assert public.id in ids
    assert private.id not in ids  # campaign-private entity never reaches the answer
    assert all(p.kind == "entity" for p in passages)


# ---------------------------------------------------------------------------
# 3. Chunk + entity hits map to RetrievedPassages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_maps_chunk_and_entity(session, monkeypatch):
    session.add(Book(id="phb", display_name="Player's Handbook"))
    chunk = KnowledgeChunk(
        book_id="phb",
        chunk_ref="c1",
        content="Fireball is a 3rd-level evocation spell.",
        section_path="Spells > Fireball",
    )
    entity = Entity(entity_type="spell", name="Fireball", is_global=True)
    session.add(chunk)
    session.add(entity)
    session.commit()
    session.refresh(chunk)
    session.refresh(entity)
    session.add(EntitySpell(entity_id=entity.id, level=3, school="evocation"))
    session.commit()

    def fake_knn(sess, vector, kinds, limit, model=None):
        return [_hit("chunk", chunk.id, 0.1), _hit("entity", entity.id, 0.3)]

    monkeypatch.setattr(retrieval, "knn_embeddings", fake_knn)
    client = _mock_client([0.1] * 1024)

    passages = await retrieval.retrieve(session, "fireball", k=6, embed_client=client)

    by_kind = {p.kind: p for p in passages}
    assert "Player's Handbook" in by_kind["chunk"].title
    assert "Fireball" in by_kind["chunk"].title
    assert "Fireball is a 3rd-level" in by_kind["chunk"].text
    assert by_kind["entity"].title == "Fireball (spell)"
    # The compact statblock folds typed detail (level/school) into the text.
    assert "level: 3" in by_kind["entity"].text
    assert "school: evocation" in by_kind["entity"].text


# ---------------------------------------------------------------------------
# 4. Fail-open behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_blank_query_returns_empty_without_embedding(session):
    client = _mock_client([0.1] * 1024)
    assert await retrieval.retrieve(session, "   ", embed_client=client) == []
    client.embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_retrieve_fails_open_on_embedder_error(session):
    client = SimpleNamespace(
        embed=AsyncMock(side_effect=RuntimeError("embedder down")),
        model="voyage-4-nano",
    )
    assert await retrieval.retrieve(session, "query", embed_client=client) == []


# ---------------------------------------------------------------------------
# 5. Hybrid retrieval: lexical entity-name match anchors a named entity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retrieve_surfaces_named_entity_via_lexical_match(session, monkeypatch):
    # The real "who is Gundren?" miss: the named NPC exists but the pure vector
    # search ranks his siblings above him, so he never reaches the answer.
    gundren = Entity(entity_type="npc", name="Gundren Rockseeker", is_global=True)
    sibling_a = Entity(entity_type="npc", name="Sildar Hallwinter", is_global=True)
    sibling_b = Entity(entity_type="npc", name="Nundro Rockseeker", is_global=True)
    session.add_all([gundren, sibling_a, sibling_b])
    session.commit()
    for e in (gundren, sibling_a, sibling_b):
        session.refresh(e)

    # Vector search returns ONLY the siblings, never Gundren himself.
    def fake_knn(sess, vector, kinds, limit, model=None):
        return [_hit("entity", sibling_a.id, 0.1), _hit("entity", sibling_b.id, 0.2)]

    monkeypatch.setattr(retrieval, "knn_embeddings", fake_knn)
    client = _mock_client([0.1] * 1024)

    passages = await retrieval.retrieve(
        session, "who is Gundren?", k=6, embed_client=client
    )

    ids = [p.ref_id for p in passages]
    # The lexical anchor surfaces Gundren and ranks him first, ahead of the
    # vector-only siblings which still appear.
    assert gundren.id in ids
    assert passages[0].ref_id == gundren.id
    assert sibling_a.id in ids
    # The anchor carries the clickable entity_type for the GROUNDED IN chip.
    assert passages[0].entity_type == "npc"


@pytest.mark.asyncio
async def test_lexical_match_still_drops_non_global_entity(session, monkeypatch):
    # A campaign-private entity whose name matches a query token is NEVER anchored:
    # the is_global gate in the lexical path holds, same as the vector path.
    private = Entity(entity_type="npc", name="Gundren the Secret", is_global=False)
    session.add(private)
    session.commit()
    session.refresh(private)

    def fake_knn(sess, vector, kinds, limit, model=None):
        return []

    monkeypatch.setattr(retrieval, "knn_embeddings", fake_knn)
    client = _mock_client([0.1] * 1024)

    passages = await retrieval.retrieve(
        session, "who is Gundren?", k=6, embed_client=client
    )
    assert private.id not in [p.ref_id for p in passages]


def test_candidate_name_tokens_drops_stopwords_keeps_proper_nouns():
    tokens = retrieval._candidate_name_tokens("Who is Gundren? Tell me about the lair")
    assert "gundren" in tokens
    # Stopwords and sub-3-char tokens are dropped.
    for dropped in ("who", "is", "tell", "me", "about", "the", "lair"):
        assert dropped not in tokens
