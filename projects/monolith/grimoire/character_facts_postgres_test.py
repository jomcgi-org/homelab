"""Real migrated fact storage enforces viewer, status, evidence and replay keys."""

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError


@pytest.mark.parametrize("trial", range(4))
@pytest.mark.parametrize("fresh", [False, True])
def test_writer_retraction_and_embedding_collection_share_lock_order(
    pg, trial, fresh
):
    """Exercise real row locks and autoflush, which SQLite cannot establish."""
    from concurrent.futures import ThreadPoolExecutor
    from datetime import UTC, datetime
    from threading import Barrier

    from sqlmodel import Session, select

    from grimoire.character_facts import FactCandidate, write_character_facts
    from grimoire.models import (
        AppUser,
        Campaign,
        CampaignMember,
        CharacterFact,
        GameSession,
        PlayerCharacter,
        SessionEvent,
    )
    from grimoire.play_embeddings import (
        collect_play_inputs,
        persist_play_vectors,
        sync_events_embeddings,
    )

    engine = create_engine(
        pg.url,
        connect_args={"options": "-c lock_timeout=5000 -c statement_timeout=10000"},
    )
    campaign = Campaign(name=f"Fact lock test {trial} fresh={fresh}")
    user = AppUser(email=f"fact-lock-{uuid4()}@example.test")
    with Session(engine) as session:
        session.add_all([campaign, user])
        session.flush()
        campaign_id, user_id = campaign.id, user.id
        pc = PlayerCharacter(campaign_id=campaign_id, character_name="Player")
        game = GameSession(campaign_id=campaign_id, status="ended")
        session.add_all([pc, game])
        session.flush()
        pc_id, game_id = pc.id, game.id
        member = CampaignMember(
            campaign_id=campaign_id,
            app_user_id=user_id,
            role="player",
            player_character_id=pc_id,
        )
        events = [
            SessionEvent(
                campaign_id=campaign_id,
                session_id=game_id,
                seq=i + 1,
                kind="narration",
                audience="table",
                body={"text": f"Evidence {i}"},
            )
            for i in range(12)
        ]
        session.add_all([member, *events])
        session.flush()
        candidates = [
            FactCandidate(statement=f"Fact {i}", evidence_event_ids=[row.id])
            for i, row in enumerate(events)
        ]
        result = write_character_facts(
            session, campaign_id, game_id, pc_id, "lock/v1", candidates
        )
        assert len(result.written) == 12 and not result.rejections
        race_candidates = (
            [
                FactCandidate(
                    statement=f"Fresh fact {i}", evidence_event_ids=[row.id]
                )
                for i, row in enumerate(events)
            ]
            if fresh
            # The replay path hits ON CONFLICT DO NOTHING; fresh statements
            # exercise a real insert racing the concurrent retraction.
            else candidates
        )
        race_version = "lock/v2" if fresh else "lock/v1"
        inputs = collect_play_inputs(session, "fact-lock-test", 10000)
        persist_play_vectors(
            session, "fact-lock-test", inputs, [[0.1] * 1024 for _ in inputs]
        )
        session.commit()

    barrier = Barrier(3)

    def run(operation):
        with Session(engine) as session:
            barrier.wait(timeout=10)
            if operation == "write":
                write_character_facts(
                    session, campaign_id, game_id, pc_id, race_version, race_candidates
                )
            elif operation == "retract":
                rows = session.exec(
                    select(SessionEvent)
                    .where(SessionEvent.campaign_id == campaign_id)
                    .order_by(SessionEvent.id)
                    .with_for_update()
                ).all()
                for row in rows:
                    row.retracted_at = datetime.now(UTC)
                sync_events_embeddings(session, rows)
            else:
                inputs = collect_play_inputs(session, "fact-lock-test", 10000)
                persist_play_vectors(
                    session, "fact-lock-test", inputs, [[0.1] * 1024 for _ in inputs]
                )
            session.commit()

    try:
        with ThreadPoolExecutor(max_workers=3) as workers:
            list(workers.map(run, ("write", "retract", "embed")))
        with Session(engine) as session:
            facts = session.exec(
                select(CharacterFact).where(CharacterFact.campaign_id == campaign_id)
            ).all()
            # A fresh insert either lands before the retraction cascade and is
            # retracted with everything else, or its evidence is already gone
            # and every candidate is rejected. Either way nothing stays active.
            assert len(facts) == 12 if not fresh else len(facts) in (12, 24)
            assert all(row.status == "retracted" for row in facts)
            assert (
                session.scalar(
                    text(
                        "SELECT count(*) FROM grimoire.embedding WHERE campaign_id = :id"
                    ),
                    {"id": campaign_id},
                )
                == 0
            )
    finally:
        with engine.begin() as connection:
            # The original campaign schema deliberately does not cascade its
            # game-session and PC foreign keys. Remove only this trial's rows
            # in dependency order before cleaning up the isolated campaign.
            connection.execute(
                text("DELETE FROM grimoire.game_session WHERE campaign_id = :id"),
                {"id": campaign_id},
            )
            connection.execute(
                text("DELETE FROM grimoire.player_character WHERE campaign_id = :id"),
                {"id": campaign_id},
            )
            connection.execute(
                text("DELETE FROM grimoire.campaign WHERE id = :id"),
                {"id": campaign_id},
            )
            connection.execute(
                text("DELETE FROM grimoire.app_user WHERE id = :id"), {"id": user_id}
            )
        engine.dispose()


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
