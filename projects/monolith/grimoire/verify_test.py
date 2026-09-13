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
from grimoire.verify import _Field, _value_grounded, verify_entities


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


def _seed_table(session: Session):
    entity = Entity(
        id="55555555-5555-5555-5555-555555555555",
        entity_type="table",
        name="Random Encounters",
        source_book="bestiary",
        detail={
            "columns": ["Roll", "Encounter"],
            "rows": {"1": "fire", "2": "cold"},
        },
    )
    chunk = KnowledgeChunk(
        id="66666666-6666-6666-6666-666666666666",
        book_id="bestiary",
        chunk_ref="random-encounters",
        content="| Roll | Encounter |\n| --- | --- |\n| 1 | fire |\n| 2 | cold |",
        seq=1,
    )
    session.add_all(
        [entity, chunk, ChunkEntityMention(chunk_id=chunk.id, entity_id=entity.id)]
    )
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


@pytest.mark.parametrize(
    ("attribute", "value", "content"),
    [
        ("speed", {"fly": 3}, "Armor Class 13. Speed 30 feet."),
        ("ability_scores", {"str": 8, "dex": 18}, "STR 18 DEX 8"),
        ("detail", {"1": "cold", "2": "fire"}, "Table: 1 fire; 2 cold"),
        (
            "actions",
            {"Claw": "9 piercing damage"},
            "Bite deals 9 piercing damage.",
        ),
    ],
)
def test_structured_evidence_keeps_field_and_row_associations(
    attribute, value, content
):
    field = _Field(f"creature.{attribute}", None, attribute, value)

    assert _value_grounded(field, value, content) is False


def test_structured_evidence_accepts_complete_supported_correction():
    field = _Field("detail.rows", None, "detail", {"1": "fire", "2": "cold"})

    assert _value_grounded(field, field.value, "Table: 1 fire; 2 cold") is True


def test_markdown_table_columns_and_rows_survive_verification(session: Session):
    entity, chunk = _seed_table(session)
    client = FakeVerifier(
        {
            "results": [
                {
                    "field": "detail.columns",
                    "verdict": "confirmed",
                    "correction": None,
                    "evidence_chunk_id": chunk.id,
                },
                {
                    "field": "detail.rows",
                    "verdict": "confirmed",
                    "correction": None,
                    "evidence_chunk_id": chunk.id,
                },
            ]
        }
    )

    summary = asyncio.run(verify_entities(session, client))

    assert session.get(Entity, entity.id).detail == {
        "columns": ["Roll", "Encounter"],
        "rows": {"1": "fire", "2": "cold"},
    }
    assert session.get(EntityVerification, (entity.id, "v1")).status == "verified"
    assert summary["entities_verified"] == 1
    assert summary["values_nulled"] == 0


def test_markdown_table_rows_reject_swapped_values():
    field = _Field("detail.rows", None, "detail", {"1": "fire", "2": "cold"})
    table = "| Roll | Encounter |\n| --- | --- |\n| 1 | cold |\n| 2 | fire |"

    assert _value_grounded(field, field.value, table) is False


def test_markdown_table_columns_require_extracted_order():
    field = _Field("detail.columns", None, "detail", ["Roll", "Encounter"])

    assert _value_grounded(field, field.value, "| Encounter | Roll |") is False


@pytest.mark.parametrize(
    ("value", "content", "expected"),
    [
        ({"walk": 30, "fly": 60}, "Speed 30 feet, flying speed 60 feet.", True),
        ({"walk": 60}, "Speed 30 feet, flying speed 60 feet.", False),
        ({"walk": 60}, "Walking speed 60 feet.", True),
    ],
)
def test_walking_speed_does_not_use_other_movement_modes(value, content, expected):
    field = _Field("creature.speed", None, "speed", value)

    assert _value_grounded(field, value, content) is expected


def test_numeric_evidence_uses_complete_number_boundaries():
    field = _Field("creature.ac", None, "ac", 13)

    assert _value_grounded(field, 13, "Armor Class 13.") is True
    assert _value_grounded(field, 13, "Armor Class 130.") is False
    assert _value_grounded(field, 13, "Armor Class 13.5.") is False


@pytest.mark.parametrize("fraction", ["1/2", "1/4", "1/8"])
def test_challenge_rating_rejects_fraction_numerator_prefix(fraction):
    field = _Field("creature.cr", None, "cr", 1)

    assert _value_grounded(field, 1, f"Challenge {fraction} (100 XP)") is False


@pytest.mark.parametrize(
    ("value", "fraction"), [(0.5, "1/2"), (0.25, "1/4"), (0.125, "1/8")]
)
def test_fractional_challenge_ratings_remain_grounded(value, fraction):
    field = _Field("creature.cr", None, "cr", value)

    assert _value_grounded(field, value, f"Challenge {fraction} (100 XP)") is True


@pytest.mark.parametrize(
    "correction",
    [
        "30",
        [30],
        {"walk": "30"},
    ],
)
def test_invalid_container_or_nested_correction_is_atomic(session: Session, correction):
    entity, chunk = _seed_creature(
        session,
        content="Speed 30 feet. Bite deals 9 piercing damage.",
        speed={"walk": 25},
    )
    client = FakeVerifier(
        {
            "results": [
                {
                    "field": "creature.speed",
                    "verdict": "corrected",
                    "correction": correction,
                    "evidence_chunk_id": chunk.id,
                },
                {
                    "field": "creature.actions",
                    "verdict": "confirmed",
                    "correction": None,
                    "evidence_chunk_id": chunk.id,
                },
            ]
        }
    )

    summary = asyncio.run(verify_entities(session, client))

    assert summary["failures"] == 1
    assert session.get(EntityCreature, entity.id).speed == {"walk": 25}
    assert session.get(EntityVerification, (entity.id, "v1")) is None


def test_non_integral_integer_stat_correction_is_atomic(session: Session):
    entity, chunk = _seed_creature(
        session,
        content="Armor Class 12.5. Bite deals 9 piercing damage.",
    )
    detail = session.get(EntityCreature, entity.id)
    detail.ac = 12
    session.add(detail)
    session.commit()
    client = FakeVerifier(
        {
            "results": [
                {
                    "field": "creature.ac",
                    "verdict": "corrected",
                    "correction": 12.5,
                    "evidence_chunk_id": chunk.id,
                },
                {
                    "field": "creature.actions",
                    "verdict": "confirmed",
                    "correction": None,
                    "evidence_chunk_id": chunk.id,
                },
            ]
        }
    )

    summary = asyncio.run(verify_entities(session, client))

    assert summary["failures"] == 1
    assert session.get(EntityCreature, entity.id).ac == 12
    assert session.get(EntityVerification, (entity.id, "v1")) is None


def test_invalid_new_nested_action_value_is_atomic(session: Session):
    entity, chunk = _seed_creature(
        session,
        content="Actions: Claw 7. Speed 25 feet.",
        speed={"walk": 25},
    )
    client = FakeVerifier(
        {
            "results": [
                {
                    "field": "creature.speed",
                    "verdict": "confirmed",
                    "correction": None,
                    "evidence_chunk_id": chunk.id,
                },
                {
                    "field": "creature.actions",
                    "verdict": "corrected",
                    "correction": {"Claw": 7},
                    "evidence_chunk_id": chunk.id,
                },
            ]
        }
    )

    summary = asyncio.run(verify_entities(session, client))

    assert summary["failures"] == 1
    assert session.get(EntityCreature, entity.id).actions == {
        "Bite": "9 piercing damage"
    }
    assert session.get(EntityVerification, (entity.id, "v1")) is None


def test_failed_entity_is_deferred_so_later_work_progresses(session: Session):
    first, _first_chunk = _seed_creature(
        session,
        content="Actions: Bite deals 9 piercing damage.",
    )
    second = Entity(
        id="33333333-3333-3333-3333-333333333333",
        entity_type="creature",
        name="Ember Drake",
        source_book="bestiary",
    )
    second_chunk = KnowledgeChunk(
        id="44444444-4444-4444-4444-444444444444",
        book_id="bestiary",
        chunk_ref="ember-drake",
        content="Actions: Claw deals 7 slashing damage.",
        seq=2,
    )
    session.add_all(
        [
            second,
            second_chunk,
            EntityCreature(entity_id=second.id, actions={"Claw": "7 slashing damage"}),
            ChunkEntityMention(chunk_id=second_chunk.id, entity_id=second.id),
        ]
    )
    session.commit()

    class FailFirstVerifier:
        model = "test-verifier"
        verifier_version = "v1"

        async def verify(self, entity_name, fields, evidence):
            if entity_name == "Ash Drake":
                raise RuntimeError("permanent failure")
            return {
                "results": [
                    {
                        "field": "creature.actions",
                        "verdict": "confirmed",
                        "correction": None,
                        "evidence_chunk_id": evidence[0]["chunk_id"],
                    }
                ]
            }

    client = FailFirstVerifier()
    assert asyncio.run(verify_entities(session, client, limit=1))["failures"] == 1
    second_run = asyncio.run(verify_entities(session, client, limit=1))

    assert session.get(EntityVerification, (first.id, "v1")) is None
    assert session.get(EntityVerification, (second.id, "v1")) is not None
    assert second_run["entities_checked"] == 1
