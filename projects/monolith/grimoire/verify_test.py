"""Hermetic regression tests for the evidence-grounded entity verifier."""

from __future__ import annotations

import asyncio

import pytest
from sqlmodel import Session, SQLModel, create_engine

from grimoire.models import (
    ChunkEntityMention,
    Entity,
    EntityCreature,
    EntityVerification,
    KnowledgeChunk,
)
from grimoire.verify import verify_entities


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'verify.sqlite'}")
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


class FakeVerifier:
    model = "test-verifier"

    def __init__(self, response: dict, version: str = "v1"):
        self.response = response
        self.verifier_version = version
        self.calls = 0

    async def verify(self, entity_name, fields, evidence):
        self.calls += 1
        return self.response


def _seed_creature(session: Session, *, content: str, speed: dict | None = None):
    entity = Entity(
        id="11111111-1111-1111-1111-111111111111",
        entity_type="creature",
        name="Ash Drake",
        source_book="bestiary",
    )
    chunk = KnowledgeChunk(
        id="22222222-2222-2222-2222-222222222222",
        book_id="bestiary",
        chunk_ref="ash-drake",
        content=content,
        seq=1,
    )
    session.add(entity)
    session.add(chunk)
    session.add(
        EntityCreature(
            entity_id=entity.id,
            speed=speed or {},
            actions={"Bite": "9 piercing damage"},
        )
    )
    session.add(ChunkEntityMention(chunk_id=chunk.id, entity_id=entity.id))
    session.commit()
    return entity, chunk


def test_correction_and_unverifiable_are_grounded_and_counted(session: Session):
    entity, chunk = _seed_creature(
        session,
        content="Armor Class 15. Speed 30 feet. Bite deals 9 piercing damage.",
        speed={"walk": 25},
    )
    client = FakeVerifier(
        {
            "results": [
                {
                    "field": "creature.speed",
                    "verdict": "corrected",
                    "correction": {"walk": 30},
                    "evidence_chunk_id": chunk.id,
                },
                {
                    "field": "creature.actions",
                    "verdict": "unverifiable",
                    "correction": None,
                    "evidence_chunk_id": None,
                },
            ]
        }
    )

    summary = asyncio.run(verify_entities(session, client))

    detail = session.get(EntityCreature, entity.id)
    marker = session.get(EntityVerification, (entity.id, "v1"))
    assert detail.speed == {"walk": 30}
    assert detail.actions is None
    assert marker.status == "corrected"
    assert marker.evidence_chunk_ids == [chunk.id]
    assert marker.corrections[0]["evidence_chunk_id"] == chunk.id
    assert summary == {
        "entities_checked": 1,
        "entities_skipped": 0,
        "entities_verified": 0,
        "entities_unverifiable": 1,
        "corrections_applied": 1,
        "values_nulled": 1,
        "failures": 0,
    }


def test_seen_version_skips_and_version_bump_requeues(session: Session):
    entity, chunk = _seed_creature(
        session,
        content="Actions: Bite deals 9 piercing damage.",
    )
    response = {
        "results": [
            {
                "field": "creature.actions",
                "verdict": "confirmed",
                "correction": None,
                "evidence_chunk_id": chunk.id,
            }
        ]
    }
    first = FakeVerifier(response, "v1")
    assert asyncio.run(verify_entities(session, first))["entities_checked"] == 1

    seen = FakeVerifier(response, "v1")
    seen_summary = asyncio.run(verify_entities(session, seen))
    assert seen_summary["entities_skipped"] == 1
    assert seen.calls == 0

    bumped = FakeVerifier(response, "v2")
    bumped_summary = asyncio.run(verify_entities(session, bumped))
    assert bumped_summary["entities_checked"] == 1
    assert bumped.calls == 1
    assert session.get(EntityVerification, (entity.id, "v2")) is not None


def test_no_marker_evidence_nulls_without_model_call(session: Session):
    entity, _chunk = _seed_creature(
        session,
        content="The drake guards the ruined gate.",
    )
    client = FakeVerifier({"results": []})

    summary = asyncio.run(verify_entities(session, client))

    assert client.calls == 0
    assert summary["values_nulled"] == 1
    assert session.get(EntityCreature, entity.id).actions is None
    assert session.get(EntityVerification, (entity.id, "v1")).status == "unverifiable"


def test_unsupported_correction_is_rejected_and_nulled(session: Session):
    entity, chunk = _seed_creature(
        session,
        content="Armor Class 15. Speed 30 feet.",
        speed={"walk": 25},
    )
    client = FakeVerifier(
        {
            "results": [
                {
                    "field": "creature.speed",
                    "verdict": "corrected",
                    "correction": {"walk": 99},
                    "evidence_chunk_id": chunk.id,
                },
                {
                    "field": "creature.actions",
                    "verdict": "unverifiable",
                    "correction": None,
                    "evidence_chunk_id": None,
                },
            ]
        }
    )

    summary = asyncio.run(verify_entities(session, client))

    detail = session.get(EntityCreature, entity.id)
    assert detail.speed is None
    assert summary["corrections_applied"] == 0
    assert summary["values_nulled"] == 2


def test_malformed_response_is_retried_next_run_without_mutation(session: Session):
    entity, _chunk = _seed_creature(
        session,
        content="Actions: Bite deals 9 piercing damage.",
    )
    client = FakeVerifier({"results": []})

    summary = asyncio.run(verify_entities(session, client))

    assert summary["failures"] == 1
    assert session.get(EntityCreature, entity.id).actions == {
        "Bite": "9 piercing damage"
    }
    assert session.get(EntityVerification, (entity.id, "v1")) is None
