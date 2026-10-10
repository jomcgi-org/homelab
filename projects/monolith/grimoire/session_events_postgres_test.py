"""Real migrations, row-lock concurrency, rollback, and private-child lifecycle."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool
from sqlmodel import Session, create_engine, select

from grimoire.audience import Audience
from grimoire.models import (
    AppUser,
    Campaign,
    CampaignMember,
    GameSession,
    PlayerCharacter,
    SessionEvent,
    TranscriptConsent,
)
from grimoire.router import UtteranceRequest, ingest_utterance, revoke_transcript_consent
from grimoire.session_events import append_event


def _delete_campaign(connection, campaign_id):
    # Legacy PCs and sessions have NO ACTION campaign FKs. Remove those
    # dependents explicitly; the new log must not block the existing order.
    connection.execute(
        delete(CampaignMember).where(CampaignMember.campaign_id == campaign_id)
    )
    connection.execute(
        delete(PlayerCharacter).where(PlayerCharacter.campaign_id == campaign_id)
    )
    connection.execute(
        delete(GameSession).where(GameSession.campaign_id == campaign_id)
    )
    connection.execute(delete(Campaign).where(Campaign.id == campaign_id))


@pytest.fixture
def lane(pg):
    engine = create_engine(pg.url, poolclass=NullPool)
    with Session(engine) as session:
        campaign = Campaign(name=f"events-{uuid4()}")
        user = AppUser(
            email=f"events-{uuid4()}@example.test", display_name="Event author"
        )
        session.add_all([campaign, user])
        session.flush()
        member = CampaignMember(campaign_id=campaign.id, app_user_id=user.id, role="dm")
        game_session = GameSession(campaign_id=campaign.id)
        pc = PlayerCharacter(campaign_id=campaign.id, character_name="Audience PC")
        session.add_all([member, game_session, pc])
        session.commit()
        info = SimpleNamespace(
            engine=engine,
            campaign_id=campaign.id,
            session_id=game_session.id,
            member_id=member.id,
            pc_id=pc.id,
            user_id=user.id,
            email=user.email,
        )
    try:
        yield info
    finally:
        with engine.begin() as connection:
            _delete_campaign(connection, info.campaign_id)
            connection.execute(delete(AppUser).where(AppUser.id == info.user_id))
        engine.dispose()


def _append(session, game_session, lane, *, audience=None, body=None):
    return append_event(
        session,
        game_session=game_session,
        kind="action",
        audience=audience or Audience("table"),
        author_member_id=lane.member_id,
        body=body or {},
    )


def test_revoke_committed_before_ingest_rejects_even_cached_consent(lane):
    with Session(lane.engine) as session:
        game_session = session.get(GameSession, lane.session_id)
        game_session.transcript_state = "on"
        consent = TranscriptConsent(
            campaign_id=lane.campaign_id, member_id=lane.member_id, processor="Local STT"
        )
        session.add(consent)
        session.commit()
        consent_id = consent.id

    cached = Event()
    revoked = Event()

    def ingest_after_revoke():
        with Session(lane.engine) as session:
            # Keep an unrevoked identity-map object across the other connection's
            # commit. The ingest must query active consent under FOR SHARE.
            old = session.get(TranscriptConsent, consent_id)
            assert old.revoked_at is None
            cached.set()
            assert revoked.wait(timeout=10)
            now = datetime.now(timezone.utc)
            with pytest.raises(HTTPException) as error:
                ingest_utterance(
                    lane.campaign_id, lane.session_id,
                    UtteranceRequest(
                        text="Must not be stored", source="browser", confidence=1,
                        started_at=now, ended_at=now,
                    ),
                    email=lane.email, session=session,
                )
            assert error.value.status_code == 403
            assert error.value.detail == "active transcript consent required"
            session.rollback()

    def revoke():
        assert cached.wait(timeout=10)
        with Session(lane.engine) as session:
            assert revoke_transcript_consent(lane.campaign_id, email=lane.email, session=session).status_code == 204
        revoked.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ingest_result = pool.submit(ingest_after_revoke)
        revoke_result = pool.submit(revoke)
        revoke_result.result(timeout=20)
        ingest_result.result(timeout=20)
    with Session(lane.engine) as session:
        assert session.get(TranscriptConsent, consent_id).revoked_at is not None
        assert session.exec(select(SessionEvent).where(SessionEvent.session_id == lane.session_id)).all() == []


def test_uuid_audience_uses_persisted_canonical_ids(lane):
    with Session(lane.engine) as session:
        row = _append(
            session,
            session.get(GameSession, lane.session_id),
            lane,
            audience=Audience("pcs", frozenset([lane.pc_id.upper()])),
        )
        assert row.audience_pc_ids == [lane.pc_id]
        session.commit()


def test_concurrent_append_gap_free_and_per_session(pg, lane):
    workers = 8
    events_per_worker = 5
    barrier = Barrier(workers)

    def writer(worker):
        # Each thread owns a separate connection and transaction.
        engine = create_engine(pg.url, poolclass=NullPool)
        lock_queries = []

        @event.listens_for(engine, "before_cursor_execute")
        def assert_lock(
            connection, cursor, statement, parameters, context, executemany
        ):
            if (
                statement.startswith("SELECT")
                and "FROM grimoire.game_session" in statement
            ):
                # This also makes lock removal fail deterministically, even on a
                # lucky scheduler that happens to serialize unlocked writers.
                assert "FOR UPDATE" in statement
                lock_queries.append(statement)

        try:
            with Session(engine) as session, session.begin():
                # No pre-lock read: all writers start with the same known ID.
                game_session = GameSession(
                    id=lane.session_id, campaign_id=lane.campaign_id
                )
                barrier.wait(timeout=10)
                seqs = [
                    _append(
                        session,
                        game_session,
                        lane,
                        body={"worker": worker, "event": index},
                    ).seq
                    for index in range(events_per_worker)
                ]
                assert len(lock_queries) == 5
                return seqs
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(writer, range(workers)))
    assert sorted(seq for result in results for seq in result) == list(range(1, 41))
    with Session(lane.engine) as session:
        events = session.exec(
            select(SessionEvent)
            .where(SessionEvent.session_id == lane.session_id)
            .order_by(SessionEvent.seq)
        ).all()
        assert [row.seq for row in events] == list(range(1, 41))
        assert len({row.id for row in events}) == 40
        assert all(row.campaign_id == lane.campaign_id for row in events)
        first = session.get(GameSession, lane.session_id)
        first.status = "ended"
        session.flush()
        second = GameSession(campaign_id=lane.campaign_id)
        session.add(second)
        session.flush()
        assert _append(session, second, lane).seq == 1
        assert _append(session, second, lane).seq == 2
        session.commit()


def test_postgres_rollback_does_not_consume_seq(lane):
    with Session(lane.engine) as session:
        game_session = session.get(GameSession, lane.session_id)
        assert _append(session, game_session, lane).seq == 1
        session.commit()
        assert _append(session, game_session, lane).seq == 2
        session.rollback()
        assert _append(session, game_session, lane).seq == 2
        assert _append(session, game_session, lane).seq == 3
        session.commit()
        assert [
            row.seq
            for row in session.exec(
                select(SessionEvent)
                .where(SessionEvent.session_id == lane.session_id)
                .order_by(SessionEvent.seq)
            )
        ] == [1, 2, 3]


@pytest.mark.parametrize(
    "changes",
    [
        "kind = 'invalid'",
        "audience = 'invalid'",
        "seq = 0",
        "seq = -1",
        "audience = 'pcs', audience_pc_ids = '[]'",
        "audience = 'table', audience_pc_ids = '[\"pc\"]'",
        "audience = 'dm', audience_pc_ids = '[\"pc\"]'",
        "audience_pc_ids = '{}'",
        "audience_pc_ids = 'null'",
        "audience_pc_ids = NULL",
        "body = NULL",
        "created_at = NULL",
        "kind = NULL",
        "audience = NULL",
        "campaign_id = NULL",
        "session_id = NULL",
        "seq = NULL",
    ],
)
def test_migration_constraints_reject_invalid_rows(lane, changes):
    with Session(lane.engine) as session:
        row = _append(session, session.get(GameSession, lane.session_id), lane)
        session.commit()
        row_id = row.id
    with pytest.raises(IntegrityError), lane.engine.begin() as connection:
        connection.execute(
            text(f"UPDATE grimoire.session_event SET {changes} WHERE id = :id"),
            {"id": row_id},
        )


def test_migration_defaults_uniqueness_and_private_grants(lane):
    with lane.engine.begin() as connection:
        values = connection.execute(
            text("""
            INSERT INTO grimoire.session_event (campaign_id, session_id, seq, kind, audience)
            VALUES (:campaign, :session, 1, 'system', 'dm')
            RETURNING id, audience_pc_ids, body, created_at, author_member_id, retracted_at
        """),
            {"campaign": lane.campaign_id, "session": lane.session_id},
        ).one()
        assert values.id is not None
        assert values.audience_pc_ids == []
        assert values.body == {}
        assert values.created_at is not None
        assert values.author_member_id is None
        assert values.retracted_at is None
        assert (
            connection.execute(
                text(
                    "SELECT has_table_privilege('public_reader', 'grimoire.session_event', 'SELECT')"
                )
            ).scalar_one()
            is False
        )
    with pytest.raises(IntegrityError), lane.engine.begin() as connection:
        connection.execute(
            text("""
            INSERT INTO grimoire.session_event (campaign_id, session_id, seq, kind, audience)
            VALUES (:campaign, :session, 1, 'system', 'dm')
        """),
            {"campaign": lane.campaign_id, "session": lane.session_id},
        )


def test_author_delete_nulls_provenance_and_wipe_preserves_events(lane):
    with Session(lane.engine) as session:
        row = _append(
            session,
            session.get(GameSession, lane.session_id),
            lane,
            audience=Audience("pcs", frozenset([lane.pc_id]), lane.member_id),
        )
        row_id = row.id
        session.commit()
    with lane.engine.begin() as connection:
        connection.execute(
            delete(CampaignMember).where(CampaignMember.id == lane.member_id)
        )
        connection.execute(
            text(
                (
                    Path(__file__).parent / "wipe" / "truncate_derived_tables.sql"
                ).read_text()
            )
        )
    with Session(lane.engine) as session:
        row = session.get(SessionEvent, row_id)
        assert row is not None
        assert row.author_member_id is None
        assert row.audience == "pcs"
        assert row.audience_pc_ids == [lane.pc_id]


@pytest.mark.parametrize("parent", [Campaign, GameSession])
def test_parent_delete_cascades_events(lane, parent):
    with Session(lane.engine) as session:
        row = _append(session, session.get(GameSession, lane.session_id), lane)
        row_id = row.id
        session.commit()
    with lane.engine.begin() as connection:
        if parent is Campaign:
            _delete_campaign(connection, lane.campaign_id)
        else:
            connection.execute(
                delete(GameSession).where(GameSession.id == lane.session_id)
            )
    with Session(lane.engine) as session:
        assert session.get(SessionEvent, row_id) is None


def test_migration_foreign_key_delete_actions(lane):
    with lane.engine.connect() as connection:
        actions = connection.execute(
            text("""
            SELECT a.attname, c.confrelid::regclass::text, c.confdeltype
            FROM pg_constraint c
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
            WHERE c.conrelid = 'grimoire.session_event'::regclass AND c.contype = 'f'
        """)
        ).all()
    assert {name: (target, action) for name, target, action in actions} == {
        "campaign_id": ("grimoire.campaign", "c"),
        "session_id": ("grimoire.game_session", "c"),
        "author_member_id": ("grimoire.campaign_member", "n"),
    }


@pytest.mark.parametrize(
    "kind",
    ["narration", "action", "roll", "reveal", "handout", "turn", "system", "utterance"],
)
def test_migration_accepts_every_literal_kind(lane, kind):
    with Session(lane.engine) as session:
        row = append_event(
            session,
            game_session=session.get(GameSession, lane.session_id),
            kind=kind,
            audience=Audience("table"),
            author_member_id=None,
            body={},
        )
        session.commit()
        assert row.kind == kind
        assert row.seq == 1


def test_journal_routes_apply_postgres_audience_predicate(lane, monkeypatch):
    from core.db import get_session
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from grimoire.access import get_authenticated_email, get_game_creator_email
    from grimoire.router import router

    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
    player_email = f"journal-{uuid4()}@example.test"
    with Session(lane.engine) as session:
        user = AppUser(email=player_email)
        session.add(user)
        session.flush()
        user_id = user.id
        player = CampaignMember(
            campaign_id=lane.campaign_id,
            app_user_id=user.id,
            role="player",
            player_character_id=lane.pc_id,
        )
        session.add(player)
        game_session = session.get(GameSession, lane.session_id)
        for audience, body in (
            (Audience("table"), {"text": "TABLE-HANDOUT"}),
            (Audience("pcs", frozenset([lane.pc_id])), {"text": "PLAYER-HANDOUT"}),
            (Audience("dm"), {"text": "DM-ONLY-HANDOUT"}),
        ):
            append_event(
                session,
                game_session=game_session,
                kind="handout",
                audience=audience,
                author_member_id=lane.member_id,
                body=body,
            )
        session.commit()

    app = FastAPI()
    app.include_router(router)

    def database():
        with Session(lane.engine) as session:
            yield session

    app.dependency_overrides[get_session] = database
    app.dependency_overrides[get_authenticated_email] = lambda: player_email
    app.dependency_overrides[get_game_creator_email] = lambda: player_email
    try:
        with TestClient(app) as client:
            for suffix in (f"/sessions/{lane.session_id}/journal", "/journal"):
                for view, expected in (("mine", 2), ("party", 1)):
                    response = client.get(
                        f"/api/grimoire/campaigns/{lane.campaign_id}{suffix}",
                        params={"view": view},
                    )
                    assert response.status_code == 200, response.text
                    assert "DM-ONLY-HANDOUT" not in response.text
                    assert lane.member_id not in response.text
                    result = response.json()
                    if suffix == "/journal":
                        result = result["sessions"][0]["journal"]
                    assert len(result["received"]) == expected
    finally:
        with lane.engine.begin() as connection:
            connection.execute(
                delete(CampaignMember).where(CampaignMember.app_user_id == user_id)
            )
            connection.execute(delete(AppUser).where(AppUser.id == user_id))
