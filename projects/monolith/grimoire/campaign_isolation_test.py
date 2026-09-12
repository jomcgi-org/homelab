"""Real-Postgres regression coverage for per-campaign Grimoire schemas."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from threading import Event

import pytest
from core.db import get_session
from fastapi import FastAPI
from fastapi.testclient import TestClient
from shared.testing.plugin import _find_migrations_dir
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, create_engine, select

from grimoire.campaign_db import CAMPAIGN_ACCESS_ROLE, campaign_session
from grimoire.models import Entity, KnowledgeGrant, PlayerCharacter
from grimoire.router import CampaignCreateRequest, create_campaign, router
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


def _expect_denied(conn, statement: str, *, match: str = "permission denied") -> None:
    savepoint = conn.begin_nested()
    try:
        with pytest.raises(DBAPIError, match=match):
            conn.execute(text(statement))
    finally:
        savepoint.rollback()


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
            conn.execute(
                text("SELECT grimoire.set_campaign_access_context(:campaign_id)"),
                {"campaign_id": campaign_a},
            )
            assert conn.execute(
                text(f'SELECT array_agg(name ORDER BY name) FROM "{schema_a}".entity')
            ).scalar_one() == ["Campaign A Sage", "Shared Sage"]
            conn.execute(
                text("SELECT grimoire.set_campaign_access_context(:campaign_id)"),
                {"campaign_id": campaign_b},
            )
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
                assert local_a.execute(text("SELECT current_user")).scalar_one() == (
                    CAMPAIGN_ACCESS_ROLE
                )
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


def test_campaign_role_has_select_only_views_and_one_campaign_state(pg):
    engine = create_engine(pg.url)
    try:
        with Session(engine) as registry:
            campaign_a = create_campaign(
                CampaignCreateRequest(name=f"privilege-a-{uuid.uuid4().hex}"), registry
            )
            campaign_b = create_campaign(
                CampaignCreateRequest(name=f"privilege-b-{uuid.uuid4().hex}"), registry
            )
            campaign_a_id = campaign_a.id
            campaign_b_id = campaign_b.id
            schema_a = campaign_a.schema_name
            schema_b = campaign_b.schema_name
            registry.execute(
                text(
                    f'INSERT INTO "{schema_b}".player_character '
                    "(campaign_id, character_name) VALUES (:campaign, 'Hidden B')"
                ),
                {"campaign": campaign_b_id},
            )
            registry.commit()

        view_columns = {
            "entity": "id",
            "entity_creature": "entity_id",
            "entity_spell": "entity_id",
            "entity_location": "entity_id",
            "entity_npc": "entity_id",
            "knowledge_chunk": "id",
            "book": "id",
            "adventure": "id",
            "chunk_extraction": "chunk_id",
            "chunk_entity_mention": "chunk_id",
            "relationship": "id",
            "embedding": "id",
            "adventure_entity": "adventure_id",
        }
        shared_tables = {
            name: column
            for name, column in view_columns.items()
            if name != "adventure_entity"
        }

        with engine.connect() as conn:
            role = CAMPAIGN_ACCESS_ROLE
            conn.execute(
                text("SELECT grimoire.set_campaign_access_context(:campaign_id)"),
                {"campaign_id": campaign_a_id},
            )
            conn.execute(text(f'SET ROLE "{role}"'))
            assert conn.execute(text("SELECT current_user")).scalar_one() == role
            _expect_denied(
                conn,
                f"SELECT grimoire.set_campaign_access_context('{campaign_b_id}'::uuid)",
            )
            _expect_denied(conn, "SELECT * FROM grimoire.campaign_access_context")

            for view, column in view_columns.items():
                conn.execute(text(f'SELECT * FROM "{schema_a}"."{view}" LIMIT 0'))
                for privilege in ("INSERT", "UPDATE", "DELETE"):
                    assert (
                        conn.execute(
                            text(
                                "SELECT has_table_privilege("
                                ":role, :relation, :privilege)"
                            ),
                            {
                                "role": role,
                                "relation": f"{schema_a}.{view}",
                                "privilege": privilege,
                            },
                        ).scalar_one()
                        is False
                    )
                _expect_denied(
                    conn,
                    f'INSERT INTO "{schema_a}"."{view}" '
                    f'SELECT * FROM "{schema_a}"."{view}" WHERE FALSE',
                    match="permission denied|cannot insert into view",
                )
                _expect_denied(
                    conn,
                    f'UPDATE "{schema_a}"."{view}" SET "{column}" = "{column}" '
                    "WHERE FALSE",
                    match="permission denied|cannot update view",
                )
                _expect_denied(
                    conn,
                    f'DELETE FROM "{schema_a}"."{view}" WHERE FALSE',
                    match="permission denied|cannot delete from view",
                )

            for table, column in shared_tables.items():
                _expect_denied(conn, f'SELECT * FROM grimoire."{table}" LIMIT 0')
                _expect_denied(
                    conn,
                    f'INSERT INTO grimoire."{table}" '
                    f'SELECT * FROM grimoire."{table}" WHERE FALSE',
                )
                _expect_denied(
                    conn,
                    f'UPDATE grimoire."{table}" SET "{column}" = "{column}" '
                    "WHERE FALSE",
                )
                _expect_denied(conn, f'DELETE FROM grimoire."{table}" WHERE FALSE')

            assert (
                conn.execute(
                    text(f'SELECT count(*) FROM "{schema_b}".player_character')
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    text(f'SELECT count(*) FROM "{schema_b}".entity')
                ).scalar_one()
                == 0
            )
            assert (
                conn.execute(
                    text(
                        f'UPDATE "{schema_b}".player_character '
                        "SET character_name = 'Leaked' RETURNING id"
                    )
                ).all()
                == []
            )
            assert (
                conn.execute(
                    text(f'DELETE FROM "{schema_b}".player_character RETURNING id')
                ).all()
                == []
            )
            _expect_denied(
                conn,
                f'INSERT INTO "{schema_b}".player_character '
                "(campaign_id, character_name) "
                f"VALUES ('{campaign_b_id}', 'Forbidden')",
                match="row-level security",
            )
            conn.execute(text("RESET ROLE"))
    finally:
        engine.dispose()


def test_one_connection_pool_serves_real_dependencies_and_chunk_routes(pg):
    engine = create_engine(pg.url, pool_size=1, max_overflow=0, pool_timeout=1)
    try:
        with Session(engine) as registry:
            campaign_a = create_campaign(
                CampaignCreateRequest(name=f"http-a-{uuid.uuid4().hex}"), registry
            )
            campaign_b = create_campaign(
                CampaignCreateRequest(name=f"http-b-{uuid.uuid4().hex}"), registry
            )
            campaign_ids = [campaign_a.id, campaign_b.id]
            chunk_id = str(uuid.uuid4())
            registry.execute(
                text(
                    "INSERT INTO grimoire.knowledge_chunk "
                    "(id, book_id, chunk_ref, content, seq) "
                    "VALUES (:id, :book, :ref, 'bounded route', 0)"
                ),
                {
                    "id": chunk_id,
                    "book": f"route-book-{uuid.uuid4().hex}",
                    "ref": f"route-ref-{uuid.uuid4().hex}",
                },
            )
            registry.commit()

        app = FastAPI()
        app.include_router(router)

        def _session_override():
            with Session(engine) as session:
                yield session

        app.dependency_overrides[get_session] = _session_override
        with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
            character_futures = [
                pool.submit(
                    client.get,
                    f"/api/grimoire/campaigns/{campaign_id}/characters",
                )
                for campaign_id in campaign_ids
            ]
            character_responses = [
                future.result(timeout=5) for future in character_futures
            ]
            assert [response.status_code for response in character_responses] == [
                200,
                200,
            ]

            chunk_futures = [
                pool.submit(
                    client.get,
                    f"/api/grimoire/chunks/{chunk_id}",
                    params={"campaign": campaign_id, "as": "dm"},
                )
                for campaign_id in campaign_ids
            ]
            chunk_responses = [future.result(timeout=5) for future in chunk_futures]
            assert [response.status_code for response in chunk_responses] == [200, 200]
            assert [response.json()["content"] for response in chunk_responses] == [
                "bounded route",
                "bounded route",
            ]
    finally:
        engine.dispose()


def test_scoped_entity_references_reject_foreign_rows_and_cascade(pg):
    engine = create_engine(pg.url)
    try:
        with Session(engine) as registry:
            campaign_a = create_campaign(
                CampaignCreateRequest(name=f"integrity-a-{uuid.uuid4().hex}"),
                registry,
            )
            campaign_b = create_campaign(
                CampaignCreateRequest(name=f"integrity-b-{uuid.uuid4().hex}"),
                registry,
            )
            shared_keep = Entity(entity_type="npc", name="Shared Keep")
            shared_delete = Entity(entity_type="npc", name="Shared Delete")
            registry.add(shared_keep)
            registry.add(shared_delete)
            registry.commit()
            registry.refresh(shared_keep)
            registry.refresh(shared_delete)
            shared_keep_id = shared_keep.id
            shared_delete_id = shared_delete.id

            with campaign_session(registry, campaign_a) as local_a:
                character = PlayerCharacter(
                    campaign_id=campaign_a.id, character_name="Integrity Alice"
                )
                local_a.add(character)
                local_a.commit()
                local_a.refresh(character)
                character_id = character.id

                local_delete = str(uuid.uuid4())
                local_keep = str(uuid.uuid4())
                local_a.execute(
                    text(
                        f'INSERT INTO "{campaign_a.schema_name}".homebrew_entity '
                        "(id, entity_type, name, source_type, is_global) VALUES "
                        "(:delete, 'npc', 'Local Delete', 'homebrew', false), "
                        "(:keep, 'npc', 'Local Keep', 'homebrew', false)"
                    ),
                    {"delete": local_delete, "keep": local_keep},
                )
                local_a.commit()

            with campaign_session(registry, campaign_b) as local_b:
                foreign_entity = str(uuid.uuid4())
                local_b.execute(
                    text(
                        f'INSERT INTO "{campaign_b.schema_name}".homebrew_entity '
                        "(id, entity_type, name, source_type, is_global) "
                        "VALUES (:id, 'npc', 'Foreign B', 'homebrew', false)"
                    ),
                    {"id": foreign_entity},
                )
                local_b.commit()

            with campaign_session(registry, campaign_a) as local_a:
                for invalid_entity in (str(uuid.uuid4()), foreign_entity):
                    with pytest.raises(DBAPIError, match="is not available"):
                        local_a.execute(
                            text(
                                f'INSERT INTO "{campaign_a.schema_name}".knowledge_grant '
                                "(campaign_id, entity_id, player_character_id, grant_scope) "
                                "VALUES (:campaign, :entity, :character, 'full')"
                            ),
                            {
                                "campaign": campaign_a.id,
                                "entity": invalid_entity,
                                "character": character_id,
                            },
                        )
                    local_a.rollback()

                    with pytest.raises(DBAPIError, match="is not available"):
                        local_a.execute(
                            text(
                                f'INSERT INTO "{campaign_a.schema_name}".homebrew_relationship '
                                "(from_entity_id, to_entity_id, rel_type) "
                                "VALUES (:from_id, :to_id, 'INVALID')"
                            ),
                            {"from_id": shared_keep_id, "to_id": invalid_entity},
                        )
                    local_a.rollback()

                local_a.execute(
                    text(
                        f'INSERT INTO "{campaign_a.schema_name}".knowledge_grant '
                        "(campaign_id, entity_id, player_character_id, grant_scope) "
                        "VALUES (:campaign, :entity, :character, 'full')"
                    ),
                    {
                        "campaign": campaign_a.id,
                        "entity": local_delete,
                        "character": character_id,
                    },
                )
                local_a.execute(
                    text(
                        f'INSERT INTO "{campaign_a.schema_name}".homebrew_relationship '
                        "(from_entity_id, to_entity_id, rel_type) "
                        "VALUES (:shared, :local, 'LOCAL_DELETE')"
                    ),
                    {"shared": shared_keep_id, "local": local_delete},
                )
                local_a.commit()
                local_a.execute(
                    text(
                        f'DELETE FROM "{campaign_a.schema_name}".homebrew_entity '
                        "WHERE id = :id"
                    ),
                    {"id": local_delete},
                )
                local_a.commit()
                assert (
                    local_a.execute(
                        text(
                            f'SELECT count(*) FROM "{campaign_a.schema_name}".knowledge_grant '
                            "WHERE entity_id = :id"
                        ),
                        {"id": local_delete},
                    ).scalar_one()
                    == 0
                )
                assert (
                    local_a.execute(
                        text(
                            f'SELECT count(*) FROM "{campaign_a.schema_name}".homebrew_relationship '
                            "WHERE from_entity_id = :id OR to_entity_id = :id"
                        ),
                        {"id": local_delete},
                    ).scalar_one()
                    == 0
                )

                local_a.execute(
                    text(
                        f'INSERT INTO "{campaign_a.schema_name}".knowledge_grant '
                        "(campaign_id, entity_id, player_character_id, grant_scope) "
                        "VALUES (:campaign, :entity, :character, 'full')"
                    ),
                    {
                        "campaign": campaign_a.id,
                        "entity": shared_delete_id,
                        "character": character_id,
                    },
                )
                local_a.execute(
                    text(
                        f'INSERT INTO "{campaign_a.schema_name}".homebrew_relationship '
                        "(from_entity_id, to_entity_id, rel_type) "
                        "VALUES (:local, :shared, 'SHARED_DELETE')"
                    ),
                    {"local": local_keep, "shared": shared_delete_id},
                )
                local_a.commit()

            registry.execute(
                text("DELETE FROM grimoire.entity WHERE id = :id"),
                {"id": shared_delete_id},
            )
            registry.commit()

            with campaign_session(registry, campaign_a) as local_a:
                assert (
                    local_a.execute(
                        text(
                            f'SELECT count(*) FROM "{campaign_a.schema_name}".knowledge_grant '
                            "WHERE entity_id = :id"
                        ),
                        {"id": shared_delete_id},
                    ).scalar_one()
                    == 0
                )
                assert (
                    local_a.execute(
                        text(
                            f'SELECT count(*) FROM "{campaign_a.schema_name}".homebrew_relationship '
                            "WHERE from_entity_id = :id OR to_entity_id = :id"
                        ),
                        {"id": shared_delete_id},
                    ).scalar_one()
                    == 0
                )
    finally:
        engine.dispose()


def test_concurrent_grant_insert_and_entity_delete_never_leave_an_orphan(pg):
    engine = create_engine(pg.url, pool_size=4, max_overflow=0)
    try:
        with Session(engine) as registry:
            campaign = create_campaign(
                CampaignCreateRequest(name=f"concurrency-{uuid.uuid4().hex}"), registry
            )
            character_id = str(uuid.uuid4())
            registry.execute(
                text(
                    f'INSERT INTO "{campaign.schema_name}".player_character '
                    "(id, campaign_id, character_name) "
                    "VALUES (:id, :campaign, 'Concurrent Alice')"
                ),
                {"id": character_id, "campaign": campaign.id},
            )
            first_entity = str(uuid.uuid4())
            second_entity = str(uuid.uuid4())
            registry.execute(
                text(
                    "INSERT INTO grimoire.entity (id, entity_type, name) VALUES "
                    "(:first, 'npc', 'Insert First'), "
                    "(:second, 'npc', 'Delete First')"
                ),
                {"first": first_entity, "second": second_entity},
            )
            registry.commit()
            role = CAMPAIGN_ACCESS_ROLE
            campaign_id = campaign.id
            schema = campaign.schema_name

        inserted = Event()
        release_insert = Event()

        def _insert_then_hold(entity_id: str) -> None:
            with engine.begin() as conn:
                conn.execute(
                    text("SELECT grimoire.set_campaign_access_context(:campaign_id)"),
                    {"campaign_id": campaign_id},
                )
                conn.execute(text(f'SET LOCAL ROLE "{role}"'))
                conn.execute(
                    text(
                        f'INSERT INTO "{schema}".knowledge_grant '
                        "(campaign_id, entity_id, player_character_id, grant_scope) "
                        "VALUES (:campaign, :entity, :character, 'full')"
                    ),
                    {
                        "campaign": campaign_id,
                        "entity": entity_id,
                        "character": character_id,
                    },
                )
                inserted.set()
                assert release_insert.wait(timeout=5)

        def _delete_entity(entity_id: str, locked: Event | None = None, release=None):
            with engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM grimoire.entity WHERE id = :id"),
                    {"id": entity_id},
                )
                if locked is not None:
                    locked.set()
                    assert release.wait(timeout=5)

        with ThreadPoolExecutor(max_workers=2) as pool:
            insert_future = pool.submit(_insert_then_hold, first_entity)
            assert inserted.wait(timeout=5)
            delete_future = pool.submit(_delete_entity, first_entity)
            with pytest.raises(TimeoutError):
                delete_future.result(timeout=0.2)
            release_insert.set()
            insert_future.result(timeout=5)
            delete_future.result(timeout=5)

        with engine.connect() as conn:
            assert (
                conn.execute(
                    text(f'SELECT count(*) FROM "{schema}".knowledge_grant')
                ).scalar_one()
                == 0
            )

        deleted = Event()
        release_delete = Event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            delete_future = pool.submit(
                _delete_entity, second_entity, deleted, release_delete
            )
            assert deleted.wait(timeout=5)
            insert_future = pool.submit(_insert_then_hold, second_entity)
            with pytest.raises(TimeoutError):
                insert_future.result(timeout=0.2)
            release_delete.set()
            delete_future.result(timeout=5)
            with pytest.raises(DBAPIError, match="is not available"):
                insert_future.result(timeout=5)

        with engine.connect() as conn:
            assert (
                conn.execute(
                    text(f'SELECT count(*) FROM "{schema}".knowledge_grant')
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
