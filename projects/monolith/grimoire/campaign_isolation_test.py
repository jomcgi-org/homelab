"""Real-Postgres regression coverage for per-campaign Grimoire schemas."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from shared.testing.plugin import _find_migrations_dir
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlmodel import Session, create_engine, select

from grimoire.campaign_db import campaign_session
from grimoire.models import Entity, KnowledgeGrant, PlayerCharacter
from grimoire.router import CampaignCreateRequest, create_campaign
from grimoire.visibility import visible_entities_query

_MIGRATION = "20260912110000_grimoire_campaign_schemas.sql"


def _isolated_database(pg):
    database = f"grimoire_isolation_{uuid.uuid4().hex}"
    url = make_url(pg.url)
    admin_engine = create_engine(url.set(database="postgres"))
    with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f'CREATE DATABASE "{database}"'))
    admin_engine.dispose()
    return database, url.set(database=database), url.set(database="postgres")


def _drop_database(database: str, admin_url) -> None:
    admin_engine = create_engine(admin_url)
    with admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :database AND pid <> pg_backend_pid()"
            ),
            {"database": database},
        )
        conn.execute(text(f'DROP DATABASE "{database}"'))
    admin_engine.dispose()


def _grimoire_migrations() -> list[Path]:
    return sorted(
        path
        for path in _find_migrations_dir().glob("*.sql")
        if "grimoire" in path.name and path.name < _MIGRATION
    )


def _apply(conn, paths: list[Path]) -> None:
    for path in paths:
        conn.execute(text(path.read_text()))


def test_existing_campaign_state_and_homebrew_migrate_without_leakage(pg):
    database, test_url, admin_url = _isolated_database(pg)
    engine = create_engine(test_url)
    campaign_a = "11111111-1111-1111-1111-111111111111"
    campaign_b = "22222222-2222-2222-2222-222222222222"
    session_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    session_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    character_a = "aaaaaaaa-1111-1111-1111-111111111111"
    character_b = "bbbbbbbb-2222-2222-2222-222222222222"
    corpus_entity = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    homebrew_entity = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    grant_a = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    grant_b = "ffffffff-ffff-ffff-ffff-ffffffffffff"

    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            _apply(conn, _grimoire_migrations())
            conn.execute(
                text(
                    "INSERT INTO grimoire.campaign (id, name) VALUES "
                    "(:a, 'A'), (:b, 'B')"
                ),
                {"a": campaign_a, "b": campaign_b},
            )
            conn.execute(
                text(
                    "INSERT INTO grimoire.game_session (id, campaign_id) VALUES "
                    "(:sa, :a), (:sb, :b)"
                ),
                {"sa": session_a, "sb": session_b, "a": campaign_a, "b": campaign_b},
            )
            conn.execute(
                text(
                    "INSERT INTO grimoire.player_character "
                    "(id, campaign_id, character_name) VALUES "
                    "(:pa, :a, 'Alice'), (:pb, :b, 'Bob')"
                ),
                {
                    "pa": character_a,
                    "pb": character_b,
                    "a": campaign_a,
                    "b": campaign_b,
                },
            )
            conn.execute(
                text(
                    "INSERT INTO grimoire.entity "
                    "(id, entity_type, name, source_type, is_global, created_in_session) "
                    "VALUES (:corpus, 'npc', 'Shared Sage', 'extracted', true, NULL), "
                    "(:homebrew, 'npc', 'Campaign A Sage', 'homebrew', false, :sa)"
                ),
                {"corpus": corpus_entity, "homebrew": homebrew_entity, "sa": session_a},
            )
            conn.execute(
                text(
                    "INSERT INTO grimoire.entity_npc (entity_id, description) "
                    "VALUES (:homebrew, 'local lore')"
                ),
                {"homebrew": homebrew_entity},
            )
            conn.execute(
                text(
                    "INSERT INTO grimoire.relationship "
                    "(from_entity_id, to_entity_id, rel_type) "
                    "VALUES (:homebrew, :corpus, 'KNOWS')"
                ),
                {"homebrew": homebrew_entity, "corpus": corpus_entity},
            )
            conn.execute(
                text(
                    "INSERT INTO grimoire.knowledge_grant "
                    "(id, campaign_id, entity_id, player_character_id, grant_scope) "
                    "VALUES (:ga, :a, :homebrew, :pa, 'full'), "
                    "(:gb, :b, :corpus, :pb, 'partial')"
                ),
                {
                    "ga": grant_a,
                    "gb": grant_b,
                    "a": campaign_a,
                    "b": campaign_b,
                    "homebrew": homebrew_entity,
                    "corpus": corpus_entity,
                    "pa": character_a,
                    "pb": character_b,
                },
            )
            migration = _find_migrations_dir() / _MIGRATION
            conn.execute(text(migration.read_text()))

        with engine.connect() as conn:
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM grimoire.entity WHERE source_type = 'homebrew'"
                    )
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    text("SELECT to_regclass('grimoire.player_character')")
                ).scalar_one()
                is None
            )
            schema_a = conn.execute(
                text("SELECT schema_name FROM grimoire.campaign WHERE id = :id"),
                {"id": campaign_a},
            ).scalar_one()
            schema_b = conn.execute(
                text("SELECT schema_name FROM grimoire.campaign WHERE id = :id"),
                {"id": campaign_b},
            ).scalar_one()
            assert conn.execute(
                text(f'SELECT array_agg(name ORDER BY name) FROM "{schema_a}".entity')
            ).scalar_one() == ["Campaign A Sage", "Shared Sage"]
            assert conn.execute(
                text(f'SELECT array_agg(name ORDER BY name) FROM "{schema_b}".entity')
            ).scalar_one() == ["Shared Sage"]
            assert (
                conn.execute(
                    text(f'SELECT count(*) FROM "{schema_a}".knowledge_grant')
                ).scalar_one()
                == 1
            )
            assert (
                conn.execute(
                    text(f'SELECT count(*) FROM "{schema_b}".knowledge_grant')
                ).scalar_one()
                == 1
            )
            assert (
                conn.execute(
                    text(f'SELECT count(*) FROM "{schema_a}".homebrew_relationship')
                ).scalar_one()
                == 1
            )
            assert (
                conn.execute(
                    text(f'SELECT count(*) FROM "{schema_b}".homebrew_relationship')
                ).scalar_one()
                == 0
            )
    finally:
        engine.dispose()
        _drop_database(database, admin_url)


def test_routed_sessions_isolate_grants_characters_transcripts_and_homebrew(pg):
    engine = create_engine(pg.url, pool_size=1, max_overflow=0)
    try:
        with Session(engine) as registry:
            campaign_a = create_campaign(CampaignCreateRequest(name="A"), registry)
            campaign_b = create_campaign(CampaignCreateRequest(name="B"), registry)

            shared = Entity(entity_type="npc", name="Shared Sage")
            registry.add(shared)
            registry.commit()
            registry.refresh(shared)
            registry.refresh(campaign_a)
            registry.refresh(campaign_b)
            registry.expunge_all()
            registry.close()

            with campaign_session(registry, campaign_a) as local_a:
                alice = PlayerCharacter(
                    campaign_id=campaign_a.id, character_name="Alice"
                )
                local_a.add(alice)
                local_a.commit()
                local_a.refresh(alice)
                local_a.add(
                    KnowledgeGrant(
                        campaign_id=campaign_a.id,
                        entity_id=shared.id,
                        player_character_id=alice.id,
                        grant_scope="full",
                    )
                )
                local_a.commit()
                homebrew_id = str(uuid.uuid4())
                local_a.execute(
                    text(
                        f'INSERT INTO "{campaign_a.schema_name}".homebrew_entity '
                        "(id, entity_type, name, source_type, is_global) "
                        "VALUES (:id, 'npc', 'Only A', 'homebrew', true)"
                    ),
                    {"id": homebrew_id},
                )
                local_a.execute(
                    text(
                        f'INSERT INTO "{campaign_a.schema_name}".game_session '
                        "(campaign_id) VALUES (:campaign_id) RETURNING id"
                    ),
                    {"campaign_id": campaign_a.id},
                ).scalar_one()
                game_session_id = local_a.execute(
                    text(
                        f'SELECT id FROM "{campaign_a.schema_name}".game_session '
                        "WHERE campaign_id = :campaign_id"
                    ),
                    {"campaign_id": campaign_a.id},
                ).scalar_one()
                local_a.execute(
                    text(
                        f'INSERT INTO "{campaign_a.schema_name}".session_transcript '
                        "(game_session_id, role, content) "
                        "VALUES (:session_id, 'dm', 'secret A')"
                    ),
                    {"session_id": game_session_id},
                )
                local_a.commit()
                visible_a = local_a.exec(
                    visible_entities_query(campaign_a.id, alice.id)
                ).all()
                assert {entity.name for entity, _grant in visible_a} == {
                    "Only A",
                    "Shared Sage",
                }

            # pool_size=1 forces physical connection reuse. Schema translation
            # still fully qualifies campaign B and cannot inherit A's route.
            with campaign_session(registry, campaign_b) as local_b:
                assert local_b.exec(select(PlayerCharacter)).all() == []
                assert local_b.exec(select(KnowledgeGrant)).all() == []
                assert [row.name for row in local_b.exec(select(Entity)).all()] == [
                    "Shared Sage"
                ]
                bob = PlayerCharacter(campaign_id=campaign_b.id, character_name="Bob")
                local_b.add(bob)
                local_b.commit()
                local_b.refresh(bob)
                visible_b = local_b.exec(
                    visible_entities_query(campaign_b.id, bob.id)
                ).all()
                assert [entity.name for entity, _grant in visible_b] == ["Shared Sage"]
                assert (
                    local_b.execute(
                        text(
                            f'SELECT count(*) FROM "{campaign_b.schema_name}".session_transcript'
                        )
                    ).scalar_one()
                    == 0
                )
    finally:
        engine.dispose()


def test_orphaned_homebrew_blocks_migration_instead_of_losing_data(pg):
    database, test_url, admin_url = _isolated_database(pg)
    engine = create_engine(test_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            _apply(conn, _grimoire_migrations())
            conn.execute(
                text(
                    "INSERT INTO grimoire.entity "
                    "(entity_type, name, source_type, is_global) "
                    "VALUES ('npc', 'Orphan', 'homebrew', false)"
                )
            )
        migration = _find_migrations_dir() / _MIGRATION
        with (
            pytest.raises(Exception, match="without a valid creating game session"),
            engine.begin() as conn,
        ):
            conn.execute(text(migration.read_text()))
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text("SELECT count(*) FROM grimoire.entity WHERE name = 'Orphan'")
                ).scalar_one()
                == 1
            )
    finally:
        engine.dispose()
        _drop_database(database, admin_url)
