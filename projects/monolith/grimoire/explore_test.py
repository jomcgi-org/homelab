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

from grimoire.explore import lens_predicate
from grimoire.models import Entity


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


class TestUnknownLens:
    def test_unconstrained_lens_returns_everything(self, session):
        _seed_entity(session, "npc", "Strahd")
        _seed_entity(session, "class", "Wizard")
        assert _lens_names(session, "everything") == {"Strahd", "Wizard"}
