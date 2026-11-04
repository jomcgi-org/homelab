"""Migrated PostgreSQL constraints and serialized source reconciliation."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.pool import NullPool
from sqlmodel import Session, create_engine, select
from shared.testing.plugin import _find_migrations_dir

from grimoire import knowledge_sources
from grimoire.models import (
    Campaign,
    CharacterSheetVersion,
    Entity,
    KnowledgeContribution,
    KnowledgeGrant,
    PlayerCharacter,
)


@pytest.fixture
def lane(pg):
    engine = create_engine(
        pg.url,
        poolclass=NullPool,
        connect_args={"options": "-c lock_timeout=10000 -c statement_timeout=15000"},
    )
    with Session(engine) as session:
        campaign = Campaign(name=f"source-test-{uuid4().hex}")
        session.add(campaign)
        session.flush()
        pc = PlayerCharacter(campaign_id=campaign.id, character_name="Test PC")
        entities = [
            Entity(
                entity_type=kind,
                name=f"{kind}-{uuid4().hex}",
                is_global=True,
                source_book="system-reference-doc-5-1",
            )
            for kind in ("race", "class")
        ]
        session.add_all([pc, *entities])
        session.flush()
        version = CharacterSheetVersion(
            campaign_id=campaign.id,
            player_character_id=pc.id,
            version=1,
            status="approved",
            created_by_email="sources@example.test",
            submitted_at=datetime.now(UTC),
            decided_at=datetime.now(UTC),
            decided_by_email="dm@example.test",
            sheet={
                "ancestry": entities[0].name,
                "class_name": entities[1].name,
                "level": 1,
            },
        )
        session.add(version)
        session.commit()
        ids = campaign.id, pc.id, version.id, [entity.id for entity in entities]
    try:
        yield engine, ids
    finally:
        # Approved history is immutable even to the fixture owner. The shared
        # pg fixture destroys this disposable database after the test session.
        engine.dispose()


def test_migrated_sources_preserve_manual_fallback_and_rollback(lane):
    engine, (campaign_id, pc_id, version_id, entity_ids) = lane
    with Session(engine) as session:
        manual = KnowledgeGrant(
            campaign_id=campaign_id,
            player_character_id=pc_id,
            entity_id=entity_ids[0],
            grant_scope="name_only",
            revealed_details={"clue": "manual survives"},
        )
        session.add(manual)
        session.flush()
        knowledge_sources.preserve_manual(session, manual)
        knowledge_sources.reconcile_sheet(
            session, session.get(CharacterSheetVersion, version_id)
        )
        assert manual.grant_scope == "full"
        session.commit()
        for _ in range(2):
            knowledge_sources.reconcile_sheet(
                session, session.get(CharacterSheetVersion, version_id)
            )
        session.commit()
        assert len(knowledge_sources.contributions(session, manual)) == 2
        auto = next(
            source
            for source in knowledge_sources.contributions(session, manual)
            if source.source_sheet_version_id
        )
        session.delete(auto)
        session.flush()
        assert knowledge_sources.recompute(session, manual)
        assert manual.grant_scope == "name_only"
        assert manual.revealed_details == {"clue": "manual survives"}
        session.rollback()
        session.refresh(manual)
        assert manual.grant_scope == "full"
        assert len(knowledge_sources.contributions(session, manual)) == 2


def test_player_lock_serializes_manual_and_automatic_sources(lane):
    engine, (campaign_id, pc_id, version_id, entity_ids) = lane
    entered, acquired = Event(), Event()

    def manual_writer():
        with Session(engine) as session:
            entered.set()
            knowledge_sources.lock_player(session, campaign_id, pc_id)
            acquired.set()
            grant = session.exec(
                select(KnowledgeGrant).where(
                    KnowledgeGrant.player_character_id == pc_id,
                    KnowledgeGrant.entity_id == entity_ids[0],
                )
            ).one()
            knowledge_sources.set_manual(
                session,
                grant,
                scope="partial",
                details={"clue": "independent"},
                game_session=None,
            )
            session.commit()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(engine) as session:
            knowledge_sources.lock_player(session, campaign_id, pc_id)
            knowledge_sources.reconcile_sheet(
                session, session.get(CharacterSheetVersion, version_id)
            )
            future = pool.submit(manual_writer)
            assert entered.wait(5)
            assert not acquired.wait(0.1)
            session.commit()
        future.result(timeout=15)
    with Session(engine) as session:
        grant = session.exec(
            select(KnowledgeGrant).where(
                KnowledgeGrant.player_character_id == pc_id,
                KnowledgeGrant.entity_id == entity_ids[0],
            )
        ).one()
        assert grant.grant_scope == "full"
        assert {
            source.source_key
            for source in knowledge_sources.contributions(session, grant)
        } == {"manual", f"sheet:{version_id}"}


def test_source_table_has_no_public_role_privileges(pg):
    engine = create_engine(pg.url)
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM information_schema.table_privileges WHERE table_schema = 'grimoire' AND table_name = 'knowledge_contribution' AND grantee IN ('PUBLIC', 'public_reader', 'public_writer')"
                    )
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()


def test_migration_backfills_manual_scope_details_and_session_without_deletion(pg):
    engine = create_engine(pg.url)
    schema = f"source_backfill_{uuid4().hex}"
    migration = (
        (_find_migrations_dir() / "20261103120000_grimoire_knowledge_sources.sql")
        .read_text()
        .replace("grimoire.", f"{schema}.")
    )
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.exec_driver_sql(f"CREATE SCHEMA {schema}")
                connection.exec_driver_sql(
                    f"CREATE TABLE {schema}.knowledge_grant (id UUID PRIMARY KEY, grant_scope TEXT NOT NULL, revealed_details JSONB, granted_in_session UUID)"
                )
                connection.exec_driver_sql(
                    f"CREATE TABLE {schema}.character_sheet_version (id UUID PRIMARY KEY)"
                )
                connection.exec_driver_sql(
                    f"INSERT INTO {schema}.knowledge_grant SELECT gen_random_uuid(), scope, '{{\"clue\": \"existing manual\"}}'::jsonb, gen_random_uuid() FROM unnest(ARRAY['name_only', 'partial', 'full']) scope"
                )
                before = connection.exec_driver_sql(
                    f"SELECT * FROM {schema}.knowledge_grant ORDER BY id"
                ).all()
                connection.exec_driver_sql(migration)
                assert (
                    connection.exec_driver_sql(
                        f"SELECT * FROM {schema}.knowledge_grant ORDER BY id"
                    ).all()
                    == before
                )
                assert (
                    connection.exec_driver_sql(
                        f"SELECT grant_id, grant_scope, revealed_details, granted_in_session FROM {schema}.knowledge_contribution WHERE source_key = 'manual' AND source_sheet_version_id IS NULL ORDER BY grant_id"
                    ).all()
                    == before
                )
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
