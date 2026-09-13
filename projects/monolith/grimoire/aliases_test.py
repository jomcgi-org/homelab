"""Hermetic tests for report-first, review-approved alias merging."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from grimoire.aliases import (
    generate_alias_candidates,
    merge_approved_aliases,
    record_alias_decision,
)
from grimoire.models import (
    ChunkEntityMention,
    Embedding,
    Entity,
    EntityAliasReview,
    EntityNpc,
    EntityVerification,
    KnowledgeChunk,
    Relationship,
)
from grimoire.verify import verify_entities


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'aliases.sqlite'}")
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


class FakeEmbedder:
    model = "test-embedding"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[str] = []

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls.extend(texts)
        if self.fail:
            raise RuntimeError("embedding unavailable")
        return [[0.7] * 1024 for _ in texts]


def _seed_pair(session: Session):
    survivor = Entity(
        id="11111111-1111-1111-1111-111111111111",
        entity_type="npc",
        name="Gundren Rockseeker",
        source_book="lost-mine",
    )
    twin = Entity(
        id="22222222-2222-2222-2222-222222222222",
        entity_type="npc",
        name="Gundren",
        source_book="lost-mine",
    )
    other = Entity(
        id="33333333-3333-3333-3333-333333333333",
        entity_type="npc",
        name="Sildar Hallwinter",
        source_book="lost-mine",
    )
    shared = KnowledgeChunk(
        id="44444444-4444-4444-4444-444444444444",
        book_id="lost-mine",
        chunk_ref="shared",
        content="Gundren Rockseeker asks Sildar to find Gundren.",
        seq=1,
    )
    twin_only = KnowledgeChunk(
        id="55555555-5555-5555-5555-555555555555",
        book_id="lost-mine",
        chunk_ref="twin-only",
        content="Gundren left before dawn.",
        seq=2,
    )
    session.add_all([survivor, twin, other, shared, twin_only])
    session.add_all(
        [
            EntityNpc(entity_id=survivor.id, occupation="miner"),
            EntityNpc(entity_id=twin.id, race="dwarf", occupation="merchant"),
            ChunkEntityMention(
                chunk_id=shared.id,
                entity_id=survivor.id,
                mention_text="Gundren Rockseeker",
            ),
            ChunkEntityMention(
                chunk_id=shared.id, entity_id=twin.id, mention_text="Gundren"
            ),
            ChunkEntityMention(
                chunk_id=twin_only.id, entity_id=twin.id, mention_text="Gundren"
            ),
            Relationship(
                id="66666666-6666-6666-6666-666666666666",
                from_entity_id=survivor.id,
                to_entity_id=other.id,
                rel_type="ALLY_OF",
                properties={"writer": "first"},
                chunk_id=shared.id,
            ),
            Relationship(
                id="77777777-7777-7777-7777-777777777777",
                from_entity_id=twin.id,
                to_entity_id=other.id,
                rel_type="ALLY_OF",
                properties={"writer": "later"},
                chunk_id=twin_only.id,
            ),
            Relationship(
                id="88888888-8888-8888-8888-888888888888",
                from_entity_id=other.id,
                to_entity_id=twin.id,
                rel_type="KNOWS",
                chunk_id=twin_only.id,
            ),
            Embedding(
                embeddable_kind="entity",
                embeddable_id=twin.id,
                model="test-embedding",
                dim=1024,
                vector=[0.1] * 1024,
            ),
        ]
    )
    session.commit()
    return survivor, twin, other, shared, twin_only


def test_candidate_generation_requires_same_scope_and_comention(session: Session):
    survivor, twin, _other, shared, _twin_only = _seed_pair(session)
    session.add_all(
        [
            Entity(
                id="99999999-9999-9999-9999-999999999991",
                entity_type="location",
                name="Shrine of Luck",
                source_book="lost-mine",
                site="phandalin",
            ),
            Entity(
                id="99999999-9999-9999-9999-999999999992",
                entity_type="location",
                name="Shrine",
                source_book="lost-mine",
                site="cragmaw",
            ),
        ]
    )
    session.add_all(
        [
            ChunkEntityMention(
                chunk_id=shared.id,
                entity_id="99999999-9999-9999-9999-999999999991",
            ),
            ChunkEntityMention(
                chunk_id=shared.id,
                entity_id="99999999-9999-9999-9999-999999999992",
            ),
        ]
    )
    session.commit()

    report = generate_alias_candidates(session)

    assert len(report) == 1
    assert report[0]["survivor_id"] == survivor.id
    assert report[0]["twin_id"] == twin.id
    assert report[0]["status"] == "pending"
    assert report[0]["evidence"] == [{"chunk_id": shared.id, "snippet": shared.content}]


def test_pending_and_rejected_pairs_never_merge(session: Session):
    survivor, twin, _other, _shared, _twin_only = _seed_pair(session)
    generate_alias_candidates(session)
    embedder = FakeEmbedder()

    pending = asyncio.run(merge_approved_aliases(session, embedder))
    assert pending == {"approved_seen": 0, "merged": 0, "failed": 0, "errors": []}

    record_alias_decision(
        session, survivor.id, twin.id, "rejected", "dm@example.test", "different NPCs"
    )
    rejected = asyncio.run(merge_approved_aliases(session, embedder))
    assert rejected["merged"] == 0
    assert session.get(Entity, twin.id) is not None
    assert embedder.calls == []


def test_approved_merge_repoints_dedupes_merges_and_reembeds(session: Session):
    survivor, twin, other, shared, twin_only = _seed_pair(session)
    generate_alias_candidates(session)
    record_alias_decision(session, survivor.id, twin.id, "approved", "dm@example.test")
    embedder = FakeEmbedder()

    summary = asyncio.run(merge_approved_aliases(session, embedder))

    assert summary == {"approved_seen": 1, "merged": 1, "failed": 0, "errors": []}
    assert session.get(Entity, twin.id) is None
    survivor_detail = session.get(EntityNpc, survivor.id)
    assert survivor_detail.race == "dwarf"
    assert survivor_detail.occupation == "miner"
    assert session.get(EntityNpc, twin.id) is None
    assert (
        session.get(ChunkEntityMention, (shared.id, survivor.id)).mention_text
        == "Gundren Rockseeker"
    )
    assert (
        session.get(ChunkEntityMention, (twin_only.id, survivor.id)).mention_text
        == "Gundren"
    )
    relationships = session.exec(select(Relationship)).all()
    assert len(relationships) == 2
    ally = next(rel for rel in relationships if rel.rel_type == "ALLY_OF")
    assert ally.from_entity_id == survivor.id
    assert ally.to_entity_id == other.id
    assert ally.properties == {"writer": "first"}
    assert ally.chunk_id == shared.id
    knows = next(rel for rel in relationships if rel.rel_type == "KNOWS")
    assert knows.to_entity_id == survivor.id
    assert (
        session.exec(
            select(Embedding).where(Embedding.embeddable_id == twin.id)
        ).first()
        is None
    )
    survivor_embedding = session.exec(
        select(Embedding).where(Embedding.embeddable_id == survivor.id)
    ).one()
    assert survivor_embedding.vector[0] == pytest.approx(0.7)
    review = session.get(EntityAliasReview, (survivor.id, twin.id))
    assert review.status == "merged"
    assert review.reviewed_by == "dm@example.test"
    assert review.merged_at is not None
    assert len(embedder.calls) == 1


def test_embed_failure_rolls_back_entire_pair(session: Session):
    survivor, twin, _other, _shared, twin_only = _seed_pair(session)
    generate_alias_candidates(session)
    record_alias_decision(session, survivor.id, twin.id, "approved", "dm@example.test")
    session.add_all(
        [
            EntityVerification(
                entity_id=survivor.id,
                verifier_version="v1",
                model="test-verifier",
                status="verified",
            ),
            EntityVerification(
                entity_id=twin.id,
                verifier_version="v1",
                model="test-verifier",
                status="corrected",
                corrections=[{"field": "npc.race", "before": None, "after": "dwarf"}],
            ),
        ]
    )
    session.commit()

    summary = asyncio.run(merge_approved_aliases(session, FakeEmbedder(fail=True)))

    assert summary["merged"] == 0
    assert summary["failed"] == 1
    assert "embedding unavailable" in summary["errors"][0]["error"]
    assert session.get(Entity, twin.id) is not None
    assert session.get(ChunkEntityMention, (twin_only.id, twin.id)) is not None
    review = session.get(EntityAliasReview, (survivor.id, twin.id))
    assert review.status == "approved"
    assert review.verification_history == []
    assert session.get(EntityVerification, (survivor.id, "v1")) is not None
    assert session.get(EntityVerification, (twin.id, "v1")) is not None


def test_merge_archives_provenance_and_reverifies_survivor_same_version(
    session: Session,
):
    survivor, twin, _other, shared, _twin_only = _seed_pair(session)
    survivor_detail = session.get(EntityNpc, survivor.id)
    survivor_detail.description = "Gundren is a miner."
    shared.content = "Traits: Gundren is a miner. Gundren Rockseeker is Gundren."
    session.add_all(
        [
            survivor_detail,
            shared,
            EntityVerification(
                entity_id=survivor.id,
                verifier_version="v1",
                model="test-verifier",
                status="verified",
                evidence_chunk_ids=[shared.id],
            ),
            EntityVerification(
                entity_id=twin.id,
                verifier_version="v1",
                model="test-verifier",
                status="corrected",
                evidence_chunk_ids=[shared.id],
                corrections=[{"field": "npc.race", "before": None, "after": "dwarf"}],
            ),
        ]
    )
    session.commit()
    generate_alias_candidates(session)
    record_alias_decision(session, survivor.id, twin.id, "approved", "dm@example.test")

    assert asyncio.run(merge_approved_aliases(session, FakeEmbedder()))["merged"] == 1

    review = session.get(EntityAliasReview, (survivor.id, twin.id))
    assert {item["entity_id"] for item in review.verification_history} == {
        survivor.id,
        twin.id,
    }
    twin_history = next(
        item for item in review.verification_history if item["entity_id"] == twin.id
    )
    assert twin_history["corrections"][0]["after"] == "dwarf"
    assert twin_history["evidence_chunk_ids"] == [shared.id]
    assert session.get(EntityVerification, (survivor.id, "v1")) is None

    class ConfirmingVerifier:
        model = "test-verifier"
        verifier_version = "v1"

        async def verify(self, entity_name, fields, evidence):
            return {
                "results": [
                    {
                        "field": "npc.description",
                        "verdict": "confirmed",
                        "correction": None,
                        "evidence_chunk_id": shared.id,
                    }
                ]
            }

    assert (
        asyncio.run(verify_entities(session, ConfirmingVerifier()))["entities_checked"]
        == 1
    )
    assert session.get(EntityVerification, (survivor.id, "v1")) is not None


def test_failed_approved_pair_is_deferred_so_later_pair_progresses(session: Session):
    survivor, twin, _other, _shared, _twin_only = _seed_pair(session)
    generate_alias_candidates(session)
    record_alias_decision(session, survivor.id, twin.id, "approved", "dm@example.test")
    stale = EntityAliasReview(
        survivor_id="99999999-9999-9999-9999-999999999998",
        twin_id="99999999-9999-9999-9999-999999999999",
        status="approved",
        reviewed_by="dm@example.test",
        reviewed_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
    )
    session.add(stale)
    session.commit()

    first_run = asyncio.run(merge_approved_aliases(session, FakeEmbedder(), limit=1))
    second_run = asyncio.run(merge_approved_aliases(session, FakeEmbedder(), limit=1))

    assert first_run["failed"] == 1
    assert second_run["merged"] == 1
    assert session.get(
        EntityAliasReview, (stale.survivor_id, stale.twin_id)
    ).status == ("approved")
    assert session.get(Entity, twin.id) is None
