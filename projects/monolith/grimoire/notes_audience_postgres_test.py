"""Run the same opt-in notes agreement oracle against real PostgreSQL."""

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from grimoire.testing.notes_matrix import assert_note_agreement, notes_table


def test_postgres_notes_agreement(pg):
    engine = create_engine(pg.url)
    table = notes_table(f"notes_matrix_{uuid4().hex}")
    try:
        table.create(engine)
        with engine.begin() as connection:
            assert_note_agreement(connection, table)
    finally:
        table.drop(engine, checkfirst=True)
        engine.dispose()


def test_real_note_migration_defaults_constraints_and_fk_lifecycle(pg):
    engine = create_engine(pg.url)
    ids = {key: str(uuid4()) for key in ("campaign", "user", "member", "pc", "session")}
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(
                    text(
                        "INSERT INTO grimoire.campaign (id, name) VALUES (:campaign, 'Notes migration')"
                    ),
                    ids,
                )
                assert (
                    connection.scalar(
                        text(
                            "SELECT notes_dm_readable_default FROM grimoire.campaign WHERE id = :campaign"
                        ),
                        ids,
                    )
                    is False
                )
                connection.execute(
                    text(
                        "INSERT INTO grimoire.app_user (id, email) VALUES (:user, :email)"
                    ),
                    {**ids, "email": f"notes-{ids['user']}@example.test"},
                )
                connection.execute(
                    text(
                        "INSERT INTO grimoire.player_character (id, campaign_id, character_name) "
                        "VALUES (:pc, :campaign, 'Notes PC')"
                    ),
                    ids,
                )
                connection.execute(
                    text(
                        "INSERT INTO grimoire.campaign_member "
                        "(id, campaign_id, app_user_id, role, player_character_id) "
                        "VALUES (:member, :campaign, :user, 'player', :pc)"
                    ),
                    ids,
                )
                connection.execute(
                    text(
                        "INSERT INTO grimoire.game_session (id, campaign_id) VALUES (:session, :campaign)"
                    ),
                    ids,
                )
                note_id = connection.scalar(
                    text(
                        "INSERT INTO grimoire.note (campaign_id, author_member_id, player_character_id, "
                        "kind, title, links, created_in_session) "
                        "VALUES (:campaign, :member, :pc, 'character', 'Private', "
                        '\'{"entity_ids":[],"event_ids":[]}\', :session) RETURNING id'
                    ),
                    ids,
                )
                row = connection.execute(
                    text("SELECT * FROM grimoire.note WHERE id = :id"), {"id": note_id}
                ).one()
                assert row.dm_readable is False and row.pinned is False
                assert row.markdown == "" and row.deleted_at is None
                assert row.created_at is not None and row.updated_at is not None
                for bad_links in (
                    "{}",
                    '{"entity_ids": []}',
                    '{"entity_ids": {}, "event_ids": []}',
                    "[]",
                ):
                    with pytest.raises(IntegrityError):
                        with connection.begin_nested():
                            connection.execute(
                                text(
                                    "INSERT INTO grimoire.note (campaign_id, kind, title, links) "
                                    "VALUES (:campaign, 'party', 'Bad', CAST(:links AS jsonb))"
                                ),
                                {**ids, "links": bad_links},
                            )
                assert (
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM information_schema.table_privileges "
                            "WHERE table_schema = 'grimoire' AND table_name = 'note' AND grantee = 'public_reader'"
                        )
                    )
                    == 0
                )
                connection.execute(
                    text("DELETE FROM grimoire.campaign_member WHERE id = :member"), ids
                )
                connection.execute(
                    text("DELETE FROM grimoire.player_character WHERE id = :pc"), ids
                )
                connection.execute(
                    text("DELETE FROM grimoire.game_session WHERE id = :session"), ids
                )
                row = connection.execute(
                    text("SELECT * FROM grimoire.note WHERE id = :id"), {"id": note_id}
                ).one()
                assert row.author_member_id is None
                assert row.player_character_id is None
                assert row.created_in_session is None
                connection.execute(
                    text("DELETE FROM grimoire.campaign WHERE id = :campaign"), ids
                )
                assert (
                    connection.scalar(
                        text("SELECT count(*) FROM grimoire.note WHERE id = :id"),
                        {"id": note_id},
                    )
                    == 0
                )
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
