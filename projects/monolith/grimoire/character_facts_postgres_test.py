"""Real migrated fact storage enforces viewer, status, evidence and replay keys."""

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError


def test_fact_migration_constraints_indexes_and_private_acl(pg):
    engine = create_engine(pg.url)
    try:
        with engine.connect() as connection:
            campaign, game, pc, evidence = (str(uuid4()) for _ in range(4))
            connection.execute(
                text(
                    "INSERT INTO grimoire.campaign (id, name) VALUES (:id, 'Facts test')"
                ),
                {"id": campaign},
            )
            connection.execute(
                text(
                    "INSERT INTO grimoire.game_session (id, campaign_id, status) VALUES (:id, :campaign, 'ended')"
                ),
                {"id": game, "campaign": campaign},
            )
            connection.execute(
                text(
                    "INSERT INTO grimoire.player_character (id, campaign_id, character_name) VALUES (:id, :campaign, 'PC')"
                ),
                {"id": pc, "campaign": campaign},
            )
            insert = text(
                "INSERT INTO grimoire.character_fact "
                "(campaign_id, session_id, player_character_id, viewer_key, statement, evidence_event_ids, extraction_version, status) "
                "VALUES (:campaign, :session, :pc, :viewer, :statement, CAST(:evidence AS uuid[]), 'test/v1', :status) RETURNING id"
            )
            base = {
                "campaign": campaign,
                "session": game,
                "pc": pc,
                "viewer": pc,
                "statement": "Valid PC fact",
                "evidence": "{" + evidence + "}",
                "status": "active",
            }
            assert connection.scalar(insert, base) is not None
            assert (
                connection.scalar(
                    insert,
                    {**base, "pc": None, "viewer": "party", "statement": "Party fact"},
                )
                is not None
            )
            for changes, constraint in (
                ({}, "character_fact_replay_key"),
                (
                    {"viewer": "party", "statement": "wrong viewer"},
                    "character_fact_viewer_chk",
                ),
                ({"pc": None, "statement": "missing PC"}, "character_fact_viewer_chk"),
                (
                    {"status": "unknown", "statement": "wrong status"},
                    "character_fact_status_chk",
                ),
                (
                    {"evidence": "{}", "statement": "no evidence"},
                    "character_fact_evidence_chk",
                ),
                (
                    {"evidence": "{NULL}", "statement": "null evidence"},
                    "character_fact_evidence_chk",
                ),
            ):
                with (
                    pytest.raises(IntegrityError, match=constraint),
                    connection.begin_nested(),
                ):
                    connection.execute(insert, {**base, **changes})
            indexes = connection.scalars(
                text(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = 'grimoire' AND tablename = 'character_fact'"
                )
            ).all()
            assert {
                "character_fact_viewer_status_idx",
                "character_fact_evidence_idx",
            }.issubset(indexes)
            assert (
                connection.scalar(
                    text(
                        "SELECT has_table_privilege('public_reader', 'grimoire.character_fact', 'SELECT')"
                    )
                )
                is False
            )
    finally:
        engine.dispose()
