"""Focused regression tests for review-approved Grimoire alias merges."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from grimoire import aliases
from grimoire.models import (
    AliasCandidate,
    ChunkEntityMention,
    Embedding,
    Entity,
    EntityNpc,
    KnowledgeChunk,
    Relationship,
)


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'aliases.db'}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

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
        engine.dispose()
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


class FakeEmbedClient:
    model = "test-embedding"

    def __init__(self):
        self.calls: list[list[str]] = []

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[0.75] * 1024 for _ in texts]


def _entity(
    session: Session,
    entity_type: str,
    name: str,
    book: str = "lmop",
    **values,
) -> Entity:
    row = Entity(
        entity_type=entity_type,
        name=name,
        source_book=book,
        **values,
    )
    session.add(row)
    session.flush()
    return row


def _chunk(session: Session, ref: str, content: str, book: str = "lmop"):
    row = KnowledgeChunk(book_id=book, chunk_ref=ref, content=content)
    session.add(row)
    session.flush()
    return row


def _mention(session: Session, chunk, entity, text: str | None = None):
    session.add(
        ChunkEntityMention(
            chunk_id=chunk.id,
            entity_id=entity.id,
            mention_text=text,
        )
    )


def _candidate(session: Session) -> AliasCandidate:
    report = aliases.generate_candidates(session)
    assert len(report["candidates"]) == 1
    return session.get(AliasCandidate, report["candidates"][0]["id"])


def _approve(session: Session, candidate: AliasCandidate):
    return aliases.approve_candidate(
        session,
        candidate.id,
        reviewer="reviewer@example.test",
        survivor_id=candidate.full_entity_id,
    )


def test_candidate_signals_and_comention_report(session: Session):
    prefix_short = _entity(session, "npc", "Gundren")
    prefix_full = _entity(session, "npc", "Gundren Rockseeker")
    subset_short = _entity(session, "npc", "Rockseeker")
    unrelated = _entity(session, "npc", "Sildar Hallwinter")
    wrong_type = _entity(session, "faction", "Gundren's Company")
    wrong_book = _entity(session, "npc", "Gundren Stonefoot", book="other")
    no_comention = _entity(session, "npc", "Gundren Forgekeeper")
    location_short = _entity(session, "location", "Hall", site="redbrand-hideout")
    location_full = _entity(session, "location", "Grand Hall", site="cragmaw-castle")
    chunk = _chunk(
        session,
        "c1",
        "Gundren Rockseeker, often called Gundren or Rockseeker, met Sildar.",
    )
    for entity in (
        prefix_short,
        prefix_full,
        subset_short,
        unrelated,
        wrong_type,
        wrong_book,
        location_short,
        location_full,
    ):
        _mention(session, chunk, entity, f"summary for {entity.name}")
    other = _chunk(session, "c2", "Gundren Forgekeeper appears elsewhere.")
    _mention(session, other, no_comention, "separate evidence")
    session.commit()

    report = aliases.generate_candidates(session)

    pairs = {
        (row["short_name"], row["full_name"]): row
        for row in report["candidates"]
        if row["status"] == "pending"
    }
    assert set(pairs) == {
        ("Gundren", "Gundren Rockseeker"),
        ("Rockseeker", "Gundren Rockseeker"),
    }
    evidence = pairs[("Gundren", "Gundren Rockseeker")]["evidence"]
    assert evidence[0]["chunk_id"] == chunk.id
    assert "Gundren Rockseeker" in evidence[0]["snippet"]
    assert evidence[0]["short_mention"] == "summary for Gundren"
    assert pairs[("Gundren", "Gundren Rockseeker")]["evidence_count"] == 1


def test_execution_requires_explicit_approval(session: Session):
    short = _entity(session, "npc", "Gundren")
    full = _entity(session, "npc", "Gundren Rockseeker")
    chunk = _chunk(session, "c1", "Gundren Rockseeker was known as Gundren.")
    _mention(session, chunk, short, "short summary")
    _mention(session, chunk, full, "full summary")
    session.commit()
    candidate = _candidate(session)
    embedder = FakeEmbedClient()

    with pytest.raises(aliases.ApprovalRequired):
        asyncio.run(aliases.execute_approved_candidate(session, candidate.id, embedder))

    assert embedder.calls == []
    assert session.get(Entity, short.id) is not None
    assert session.get(Entity, full.id) is not None


def test_merge_rewrites_mentions_edges_and_typed_detail(session: Session):
    short = _entity(session, "npc", "Gundren")
    full = _entity(session, "npc", "Gundren Rockseeker")
    target = _entity(session, "location", "Wave Echo Cave")
    session.add(EntityNpc(entity_id=short.id, race="dwarf", occupation="miner"))
    session.add(
        EntityNpc(entity_id=full.id, occupation="businessperson", description="full")
    )
    shared = _chunk(session, "c1", "Gundren Rockseeker, called Gundren, found a cave.")
    short_only = _chunk(session, "c2", "Gundren guarded the map.")
    _mention(session, shared, short, "short summary")
    _mention(session, shared, full, "full summary")
    _mention(session, short_only, short, "map summary")
    session.add_all(
        [
            Relationship(
                from_entity_id=short.id,
                to_entity_id=target.id,
                rel_type="FOUND",
                properties={"short": True, "conflict": "short"},
                chunk_id=shared.id,
            ),
            Relationship(
                from_entity_id=full.id,
                to_entity_id=target.id,
                rel_type="FOUND",
                properties={"full": True, "conflict": "full"},
                chunk_id=short_only.id,
            ),
            Relationship(
                from_entity_id=target.id,
                to_entity_id=short.id,
                rel_type="KNOWN_BY",
            ),
            Relationship(
                from_entity_id=short.id,
                to_entity_id=full.id,
                rel_type="RELATED_TO",
            ),
        ]
    )
    session.add_all(
        [
            Embedding(
                embeddable_kind="entity",
                embeddable_id=short.id,
                model="old",
                dim=1024,
                vector=[0.1] * 1024,
            ),
            Embedding(
                embeddable_kind="entity",
                embeddable_id=full.id,
                model="old",
                dim=1024,
                vector=[0.2] * 1024,
            ),
            Embedding(
                embeddable_kind="entity",
                embeddable_id=full.id,
                model="older",
                dim=1024,
                vector=[0.3] * 1024,
            ),
        ]
    )
    session.commit()
    candidate = _candidate(session)
    _approve(session, candidate)
    embedder = FakeEmbedClient()

    result = asyncio.run(
        aliases.execute_approved_candidate(session, candidate.id, embedder)
    )

    assert result["embedding_refreshed"] is True
    assert session.get(Entity, short.id) is None
    assert session.get(Entity, full.id) is not None
    mentions = session.exec(select(ChunkEntityMention)).all()
    assert {(row.chunk_id, row.entity_id) for row in mentions} == {
        (shared.id, full.id),
        (short_only.id, full.id),
    }
    shared_mention = session.get(ChunkEntityMention, (shared.id, full.id))
    assert shared_mention.mention_text == "full summary\nshort summary"

    edges = session.exec(select(Relationship)).all()
    assert {(row.from_entity_id, row.to_entity_id, row.rel_type) for row in edges} == {
        (full.id, target.id, "FOUND"),
        (target.id, full.id, "KNOWN_BY"),
    }
    found = next(row for row in edges if row.rel_type == "FOUND")
    assert found.properties["full"] is True
    assert found.properties["short"] is True
    assert found.properties["conflict"] == "full"
    assert found.properties["_alias_merge_conflicts"]["conflict"] == [
        "full",
        "short",
    ]
    assert found.properties["_alias_merge_chunk_ids"] == sorted(
        [shared.id, short_only.id]
    )

    detail = session.get(EntityNpc, full.id)
    assert detail.race == "dwarf"
    assert detail.occupation == "businessperson"
    assert detail.description == "full"
    embeddings = session.exec(
        select(Embedding).where(Embedding.embeddable_kind == "entity")
    ).all()
    assert [(row.embeddable_id, row.model) for row in embeddings] == [
        (full.id, "test-embedding")
    ]
    assert embedder.calls == [
        ["Gundren Rockseeker: full summary | map summary | short summary"]
    ]


def test_generic_detail_merge_is_recursive_and_survivor_wins(session: Session):
    short = _entity(
        session,
        "item",
        "Orb",
        detail={"stats": {"charges": 3, "range": 30}, "owner": "short"},
    )
    full = _entity(
        session,
        "item",
        "Orb of Doom",
        detail={"stats": {"charges": 5}, "owner": "full"},
    )
    chunk = _chunk(session, "c1", "The Orb of Doom is also called the Orb.")
    _mention(session, chunk, short, "orb summary")
    _mention(session, chunk, full, "doom summary")
    session.commit()
    candidate = _candidate(session)
    _approve(session, candidate)

    asyncio.run(
        aliases.execute_approved_candidate(session, candidate.id, FakeEmbedClient())
    )

    survivor = session.get(Entity, full.id)
    assert survivor.detail == {
        "stats": {"charges": 5, "range": 30},
        "owner": "full",
    }


def test_failure_rolls_back_and_leaves_approval_retryable(
    session: Session, monkeypatch
):
    short = _entity(session, "npc", "Gundren")
    full = _entity(session, "npc", "Gundren Rockseeker")
    chunk = _chunk(session, "c1", "Gundren Rockseeker was called Gundren.")
    _mention(session, chunk, short, "short summary")
    _mention(session, chunk, full, "full summary")
    session.commit()
    candidate = _candidate(session)
    _approve(session, candidate)

    def fail_relationship_rewrite(*_args):
        raise RuntimeError("forced database-stage failure")

    monkeypatch.setattr(aliases, "_rewrite_relationships", fail_relationship_rewrite)
    with pytest.raises(RuntimeError, match="forced database-stage failure"):
        asyncio.run(
            aliases.execute_approved_candidate(session, candidate.id, FakeEmbedClient())
        )

    assert session.get(Entity, short.id) is not None
    assert session.get(Entity, full.id) is not None
    assert len(session.exec(select(ChunkEntityMention)).all()) == 2
    assert session.get(AliasCandidate, candidate.id).status == "approved"


def test_embedding_failure_happens_before_database_mutation(session: Session):
    short = _entity(session, "npc", "Gundren")
    full = _entity(session, "npc", "Gundren Rockseeker")
    chunk = _chunk(session, "c1", "Gundren Rockseeker was called Gundren.")
    _mention(session, chunk, short, "short summary")
    _mention(session, chunk, full, "full summary")
    session.commit()
    candidate = _candidate(session)
    _approve(session, candidate)

    class FailingEmbedClient:
        model = "test-embedding"

        async def embed_batch(self, _texts):
            raise RuntimeError("embedding unavailable")

    with pytest.raises(RuntimeError, match="embedding unavailable"):
        asyncio.run(
            aliases.execute_approved_candidate(
                session, candidate.id, FailingEmbedClient()
            )
        )

    assert session.get(Entity, short.id) is not None
    assert session.get(Entity, full.id) is not None
    assert session.get(AliasCandidate, candidate.id).status == "approved"


def test_changed_entity_makes_approval_stale_without_embedding(session: Session):
    short = _entity(session, "npc", "Gundren")
    full = _entity(session, "npc", "Gundren Rockseeker")
    chunk = _chunk(session, "c1", "Gundren Rockseeker was called Gundren.")
    _mention(session, chunk, short, "short summary")
    _mention(session, chunk, full, "full summary")
    session.commit()
    candidate = _candidate(session)
    _approve(session, candidate)
    short.source_book = "changed-book"
    session.commit()
    embedder = FakeEmbedClient()

    with pytest.raises(aliases.StaleApproval, match="same type and book"):
        asyncio.run(aliases.execute_approved_candidate(session, candidate.id, embedder))

    assert embedder.calls == []
    assert session.get(AliasCandidate, candidate.id).status == "stale"
    assert session.get(Entity, short.id) is not None


def test_changed_evidence_text_makes_approval_stale(session: Session):
    short = _entity(session, "npc", "Gundren")
    full = _entity(session, "npc", "Gundren Rockseeker")
    chunk = _chunk(session, "c1", "Gundren Rockseeker was called Gundren.")
    _mention(session, chunk, short, "short summary")
    _mention(session, chunk, full, "full summary")
    session.commit()
    candidate = _candidate(session)
    _approve(session, candidate)
    chunk.content = "The reviewed source text changed after approval."
    session.commit()
    embedder = FakeEmbedClient()

    with pytest.raises(aliases.StaleApproval, match="approval is stale"):
        asyncio.run(aliases.execute_approved_candidate(session, candidate.id, embedder))

    assert embedder.calls == []
    assert session.get(AliasCandidate, candidate.id).status == "stale"
    assert session.get(Entity, short.id) is not None


def test_successful_execution_is_replay_safe(session: Session):
    short = _entity(session, "npc", "Gundren")
    full = _entity(session, "npc", "Gundren Rockseeker")
    chunk = _chunk(session, "c1", "Gundren Rockseeker was called Gundren.")
    _mention(session, chunk, short, "short summary")
    _mention(session, chunk, full, "full summary")
    session.commit()
    candidate = _candidate(session)
    _approve(session, candidate)
    embedder = FakeEmbedClient()

    first = asyncio.run(
        aliases.execute_approved_candidate(session, candidate.id, embedder)
    )
    second = asyncio.run(
        aliases.execute_approved_candidate(session, candidate.id, embedder)
    )

    assert first["replay"] is False
    assert second == {"status": "merged", "candidate_id": candidate.id, "replay": True}
    assert len(embedder.calls) == 1
    assert session.get(AliasCandidate, candidate.id).status == "merged"
