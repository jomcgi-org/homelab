"""Tests for grimoire/explore.py's lens_predicate.

Pure predicate tests: drive `select(Entity).where(lens_predicate(...))`
directly against an in-memory SQLite fixture, no FastAPI/router involved
(mirrors the SQLite create_all + schema-strip pattern in
router_public_test.py). The `category` STORED generated column derives
correctly under SQLite's create_all, so lens predicates that filter on
`Entity.category` are exercised the same as they would be against Postgres.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from grimoire.explore import ego_subgraph, lens_predicate
from grimoire.models import ChunkEntityMention, Entity, KnowledgeChunk, Relationship


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
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


def _seed_entity(
    session, entity_type: str, name: str, *, temporality: str | None = None
):
    """A single is_global entity; category derives from entity_type via the
    Computed() column (see models._ENTITY_CATEGORY_EXPR)."""
    entity = Entity(
        entity_type=entity_type, name=name, temporality=temporality, is_global=True
    )
    session.add(entity)
    session.commit()
    session.refresh(entity)
    return entity


def _lens_names(session, lens: str) -> set[str]:
    return {
        e.name for e in session.exec(select(Entity).where(lens_predicate(lens))).all()
    }


class TestWorldLens:
    def test_includes_lore_and_historical_events_excludes_future_and_mechanics(
        self, session
    ):
        _seed_entity(session, "npc", "Strahd")
        _seed_entity(session, "event", "Fall of Barovia", temporality="historical")
        _seed_entity(session, "event", "The Ceremony", temporality="future")
        _seed_entity(session, "class", "Wizard")

        types = {
            e.entity_type
            for e in session.exec(select(Entity).where(lens_predicate("world"))).all()
        }
        # future event + mechanics (class) excluded.
        assert types == {"npc", "event"}
        assert _lens_names(session, "world") == {"Strahd", "Fall of Barovia"}


class TestStoryLens:
    def test_all_events_regardless_of_temporality(self, session):
        _seed_entity(session, "event", "Fall of Barovia", temporality="historical")
        _seed_entity(session, "event", "The Ceremony", temporality="future")
        _seed_entity(session, "npc", "Strahd")
        assert _lens_names(session, "story") == {"Fall of Barovia", "The Ceremony"}


class TestQuestsLens:
    def test_all_quests_regardless_of_temporality(self, session):
        _seed_entity(session, "quest", "Retrieve the Sunsword", temporality="present")
        _seed_entity(session, "quest", "Defeat Strahd", temporality="future")
        _seed_entity(session, "event", "Fall of Barovia", temporality="historical")
        assert _lens_names(session, "quests") == {
            "Retrieve the Sunsword",
            "Defeat Strahd",
        }


class TestRulesLens:
    def test_rules_lens_unions_spell(self, session):
        _seed_entity(session, "class", "Wizard")
        _seed_entity(session, "spell", "Fireball")
        _seed_entity(session, "npc", "Strahd")
        assert _lens_names(session, "rules") == {"Wizard", "Fireball"}


class TestEgoSubgraphImageChunkId:
    def test_focus_entity_gets_earliest_image_mention(self, session):
        """The focus node's card carries image_chunk_id; a neighbor's does
        not, even when the neighbor also has an image mention."""
        strahd = _seed_entity(session, "npc", "Strahd")
        ireena = _seed_entity(session, "npc", "Ireena")
        session.add(
            Relationship(
                from_entity_id=strahd.id, to_entity_id=ireena.id, rel_type="pursues"
            )
        )
        text_chunk = KnowledgeChunk(
            book_id="cos", chunk_ref="r0", content="Strahd broods.", seq=0
        )
        image_chunk = KnowledgeChunk(
            book_id="cos",
            chunk_ref="r1",
            content="A portrait of Strahd.",
            seq=1,
            image_ref="s3://grimoire/books/cos/img/strahd.png",
        )
        neighbor_image_chunk = KnowledgeChunk(
            book_id="cos",
            chunk_ref="r2",
            content="A portrait of Ireena.",
            seq=2,
            image_ref="s3://grimoire/books/cos/img/ireena.png",
        )
        session.add_all([text_chunk, image_chunk, neighbor_image_chunk])
        session.commit()
        session.add_all(
            [
                ChunkEntityMention(
                    chunk_id=text_chunk.id, entity_id=strahd.id, mention_text="Strahd"
                ),
                ChunkEntityMention(
                    chunk_id=image_chunk.id, entity_id=strahd.id, mention_text="Strahd"
                ),
                ChunkEntityMention(
                    chunk_id=neighbor_image_chunk.id,
                    entity_id=ireena.id,
                    mention_text="Ireena",
                ),
            ]
        )
        session.commit()

        graph = ego_subgraph(session, strahd.id)
        by_id = {node["id"]: node for node in graph["nodes"]}
        assert by_id[strahd.id]["image_chunk_id"] == image_chunk.id
        assert "image_chunk_id" not in by_id[ireena.id]

    def test_focus_entity_without_image_mention_gets_none(self, session):
        strahd = _seed_entity(session, "npc", "Strahd")
        graph = ego_subgraph(session, strahd.id)
        by_id = {node["id"]: node for node in graph["nodes"]}
        assert by_id[strahd.id]["image_chunk_id"] is None


class TestUnknownLens:
    def test_unconstrained_lens_returns_everything(self, session):
        _seed_entity(session, "npc", "Strahd")
        _seed_entity(session, "class", "Wizard")
        assert _lens_names(session, "everything") == {"Strahd", "Wizard"}
